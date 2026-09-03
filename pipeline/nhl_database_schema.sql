/*
    NHL Hockey Analytics Database
    Schema build script — derived from NHL_SQL_Server_Database_Design_Report_Revised.pdf,
    with the six design gaps/inconsistencies resolved as agreed:

      1. Added Ingestion.RawApiResponses and Ingestion.IngestionRuns (report referenced these
         in the workflow but never defined them).
      2. Added a uniqueness constraint to Game.Shifts so re-ingestion is idempotent.
      3. Added a filtered unique index so at most one CalculationVersion can be IsActive=1
         per MetricCode — this is the "configured model" the ingestion workflow runs.
      4. Plays/Shots/Goals keep the raw NHL situationCode string as-ingested (StrengthCode).
         Added Reference.SituationCodeMap to parse that raw code into skater/goalie counts.
         Resolving a *specific team's* situation (e.g. 5v4 vs 4v5 for the same raw code,
         depending on whether that team is home or away) is a calculation-layer concern,
         not something a static one-to-one lookup can express correctly, so it is NOT baked
         into this map — the calc layer compares AwaySkaters/HomeSkaters against
         Situations.StrengthHome/StrengthAway plus the team's home/away flag.
      5. Plays and Shots coordinates both standardized to DECIMAL(6,2). Plays holds the raw
         as-ingested coordinate for any event type; Shots holds the normalized
         (attacking-right) coordinate used for distance/angle/xG.
      6. Game.Shifts is ingested directly from the shift-charts endpoint. Game.PlayOnIcePlayers
         is NOT separately ingested — it is derived by joining Shifts against each play's
         period/time during ingestion workflow step 8.

    Naming: schemas (Reference, Game, Stats, Analytics, Ingestion) live inside a dedicated
    `NHLStats` database rather than a separate `nhl` database (the report's
    `nhl.Reference.Seasons`-style names are 3-part database.schema.table identifiers), and
    rather than the `model` database used earlier in development -- NHLStats keeps this
    project's data separate from whatever else lives in `model`. `model` still has an
    earlier, unused copy of this same schema/seed data from initial development; it was
    left in place rather than dropped.

    Scope: V1 targets a single season, sourced from the NHL play-by-play and shift-charts
    endpoints, with a naive/placeholder xG model (see seed data at the bottom).

    Review this script before executing it. Tables are created in dependency order:
    Reference -> Game -> Stats -> Analytics -> Ingestion.
*/

IF NOT EXISTS (SELECT 1 FROM sys.databases WHERE name = 'NHLStats')
    CREATE DATABASE NHLStats;
GO

USE NHLStats;
GO

-- ============================================================================
-- 0. Schemas
-- ============================================================================

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'Reference')
    EXEC('CREATE SCHEMA Reference');
GO
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'Game')
    EXEC('CREATE SCHEMA Game');
GO
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'Stats')
    EXEC('CREATE SCHEMA Stats');
GO
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'Analytics')
    EXEC('CREATE SCHEMA Analytics');
GO
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'Ingestion')
    EXEC('CREATE SCHEMA Ingestion');
GO
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'Projections')
    EXEC('CREATE SCHEMA Projections');
GO
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'Fantasy')
    EXEC('CREATE SCHEMA Fantasy');
GO

-- ============================================================================
-- 1. Reference schema
-- ============================================================================

CREATE TABLE Reference.Seasons
(
    SeasonID        INT IDENTITY(1,1) NOT NULL,
    NHLSeasonID     INT NOT NULL,
    StartYear       SMALLINT NOT NULL,
    EndYear         SMALLINT NOT NULL,
    DisplayName     VARCHAR(9) NOT NULL,
    CONSTRAINT PK_Seasons PRIMARY KEY (SeasonID),
    CONSTRAINT UQ_Seasons_NHLSeasonID UNIQUE (NHLSeasonID)
);
GO

CREATE TABLE Reference.Teams
(
    TeamID          INT IDENTITY(1,1) NOT NULL,
    NHLTeamID       INT NOT NULL,
    Abbreviation    VARCHAR(5) NOT NULL,
    TeamName        VARCHAR(100) NOT NULL,
    Location        VARCHAR(100) NULL,
    Conference      VARCHAR(20) NULL,
    Division        VARCHAR(30) NULL,
    Active          BIT NOT NULL DEFAULT 1,
    CONSTRAINT PK_Teams PRIMARY KEY (TeamID),
    CONSTRAINT UQ_Teams_NHLTeamID UNIQUE (NHLTeamID)
);
GO

CREATE TABLE Reference.Players
(
    PlayerID        BIGINT IDENTITY(1,1) NOT NULL,
    NHLPlayerID     INT NOT NULL,
    FirstName       VARCHAR(100) NULL,
    LastName        VARCHAR(100) NULL,
    FullName        VARCHAR(200) NOT NULL,
    PositionCode    VARCHAR(5) NULL,
    Shoots          CHAR(1) NULL,
    BirthDate       DATE NULL,
    HeightInches    DECIMAL(5,2) NULL,
    WeightLbs       DECIMAL(6,2) NULL,
    Active          BIT NOT NULL DEFAULT 1,
    CONSTRAINT PK_Players PRIMARY KEY (PlayerID),
    CONSTRAINT UQ_Players_NHLPlayerID UNIQUE (NHLPlayerID)
);
GO

CREATE TABLE Reference.PlayerTeamHistory
(
    PlayerTeamHistoryID    BIGINT IDENTITY(1,1) NOT NULL,
    PlayerID               BIGINT NOT NULL,
    TeamID                 INT NOT NULL,
    SeasonID               INT NOT NULL,
    StartDate              DATE NULL,
    EndDate                DATE NULL,
    CONSTRAINT PK_PlayerTeamHistory PRIMARY KEY (PlayerTeamHistoryID),
    CONSTRAINT FK_PTH_Player FOREIGN KEY (PlayerID)
        REFERENCES Reference.Players(PlayerID),
    CONSTRAINT FK_PTH_Team FOREIGN KEY (TeamID)
        REFERENCES Reference.Teams(TeamID),
    CONSTRAINT FK_PTH_Season FOREIGN KEY (SeasonID)
        REFERENCES Reference.Seasons(SeasonID)
);
GO

CREATE TABLE Reference.Situations
(
    SituationID         INT IDENTITY(1,1) NOT NULL,
    SituationCode       VARCHAR(20) NOT NULL,
    Description         VARCHAR(100) NOT NULL,
    StrengthHome        TINYINT NULL,
    StrengthAway        TINYINT NULL,
    IncludeEmptyNet     BIT NOT NULL DEFAULT 1,
    CONSTRAINT PK_Situations PRIMARY KEY (SituationID),
    CONSTRAINT UQ_Situations_Code UNIQUE (SituationCode)
);
GO

-- Resolves gap #4: parses the NHL API's raw 4-digit situationCode (e.g. "1551") into its
-- component skater/goalie counts. Does NOT map directly to a SituationID, because the same
-- raw code represents a different team-relative situation (e.g. 5v4 vs 4v5) depending on
-- whether the team being analyzed is home or away. The calculation layer resolves that by
-- comparing these counts + home/away flag against Situations.StrengthHome/StrengthAway.
CREATE TABLE Reference.SituationCodeMap
(
    SituationCodeMapID     INT IDENTITY(1,1) NOT NULL,
    RawSituationCode       VARCHAR(20) NOT NULL,
    AwayGoalieInNet        BIT NOT NULL,
    AwaySkaters            TINYINT NOT NULL,
    HomeSkaters            TINYINT NOT NULL,
    HomeGoalieInNet        BIT NOT NULL,
    CONSTRAINT PK_SituationCodeMap PRIMARY KEY (SituationCodeMapID),
    CONSTRAINT UQ_SituationCodeMap_Code UNIQUE (RawSituationCode)
);
GO

-- The full season schedule, including games that haven't been played yet -- deliberately
-- NOT part of Game.Games, which only ever holds fully-ingested games (run_game() requires
-- boxscore/play-by-play/shift data that doesn't exist yet for a future game; GameStatus
-- there is always 'OFF'). NHLGameID isn't FK'd to Game.Games for the same reason: a
-- Schedule row is written the moment the season schedule is known, long before that game
-- has a Game.Games row (or ever will, for a still-upcoming game).
CREATE TABLE Reference.Schedule
(
    ScheduleID      BIGINT IDENTITY(1,1) NOT NULL,
    SeasonID        INT NOT NULL,
    NHLGameID       INT NOT NULL,
    GameType        VARCHAR(20) NULL,
    GameDate        DATE NOT NULL,
    StartTimeUTC    DATETIME2(0) NULL,
    HomeTeamID      INT NOT NULL,
    AwayTeamID      INT NOT NULL,
    GameState       VARCHAR(30) NULL,      -- e.g. 'FUT', 'LIVE', 'OFF' (schedule endpoint's own state, not Game.Games.GameStatus)
    CreatedAt       DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_Schedule PRIMARY KEY (ScheduleID),
    CONSTRAINT UQ_Schedule_NHLGameID UNIQUE (NHLGameID),
    CONSTRAINT FK_Schedule_Season FOREIGN KEY (SeasonID)
        REFERENCES Reference.Seasons(SeasonID),
    CONSTRAINT FK_Schedule_HomeTeam FOREIGN KEY (HomeTeamID)
        REFERENCES Reference.Teams(TeamID),
    CONSTRAINT FK_Schedule_AwayTeam FOREIGN KEY (AwayTeamID)
        REFERENCES Reference.Teams(TeamID)
);
GO

-- ============================================================================
-- 2. Game schema
-- ============================================================================

CREATE TABLE Game.Games
(
    GameID          BIGINT IDENTITY(1,1) NOT NULL,
    NHLGameID       INT NOT NULL,
    SeasonID        INT NOT NULL,
    GameType        VARCHAR(20) NOT NULL,
    GameDate        DATE NOT NULL,
    StartTimeUTC    DATETIME2(0) NULL,
    HomeTeamID      INT NOT NULL,
    AwayTeamID      INT NOT NULL,
    HomeScore       SMALLINT NULL,
    AwayScore       SMALLINT NULL,
    Venue           VARCHAR(200) NULL,
    GameStatus      VARCHAR(30) NULL,
    CONSTRAINT PK_Games PRIMARY KEY (GameID),
    CONSTRAINT UQ_Games_NHLGameID UNIQUE (NHLGameID),
    CONSTRAINT FK_Games_Season FOREIGN KEY (SeasonID)
        REFERENCES Reference.Seasons(SeasonID),
    CONSTRAINT FK_Games_HomeTeam FOREIGN KEY (HomeTeamID)
        REFERENCES Reference.Teams(TeamID),
    CONSTRAINT FK_Games_AwayTeam FOREIGN KEY (AwayTeamID)
        REFERENCES Reference.Teams(TeamID)
);
GO

CREATE TABLE Game.Plays
(
    PlayID                  BIGINT IDENTITY(1,1) NOT NULL,
    GameID                  BIGINT NOT NULL,
    NHLPlayID               INT NOT NULL,
    PeriodNumber            TINYINT NOT NULL,
    PeriodTimeSeconds       SMALLINT NULL,
    PeriodTimeRemaining     SMALLINT NULL,
    EventType               VARCHAR(50) NOT NULL,
    Description             VARCHAR(1000) NULL,
    TeamID                  INT NULL,
    PlayerID                BIGINT NULL,
    SecondaryPlayerID       BIGINT NULL,
    XCoordinate             DECIMAL(6,2) NULL,
    YCoordinate             DECIMAL(6,2) NULL,
    HomeScore               SMALLINT NULL,
    AwayScore               SMALLINT NULL,
    StrengthCode            VARCHAR(20) NULL,   -- raw NHL situationCode as ingested; see Reference.SituationCodeMap
    CreatedAt               DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_Plays PRIMARY KEY (PlayID),
    CONSTRAINT UQ_Plays_Game_NHLPlay UNIQUE (GameID, NHLPlayID),
    CONSTRAINT FK_Plays_Game FOREIGN KEY (GameID)
        REFERENCES Game.Games(GameID),
    CONSTRAINT FK_Plays_Team FOREIGN KEY (TeamID)
        REFERENCES Reference.Teams(TeamID),
    CONSTRAINT FK_Plays_Player FOREIGN KEY (PlayerID)
        REFERENCES Reference.Players(PlayerID),
    CONSTRAINT FK_Plays_SecondaryPlayer FOREIGN KEY (SecondaryPlayerID)
        REFERENCES Reference.Players(PlayerID)
);
GO

CREATE TABLE Game.Shots
(
    ShotID              BIGINT IDENTITY(1,1) NOT NULL,
    PlayID              BIGINT NOT NULL,
    GameID              BIGINT NOT NULL,
    TeamID              INT NOT NULL,
    ShooterPlayerID     BIGINT NULL,
    GoaliePlayerID      BIGINT NULL,
    PeriodNumber        TINYINT NOT NULL,
    PeriodTimeSeconds   SMALLINT NULL,
    ShotEventType       VARCHAR(30) NOT NULL,
    ShotType            VARCHAR(50) NULL,
    XCoordinate         DECIMAL(6,2) NULL,      -- normalized (attacking-right) coordinate; Plays holds the raw one
    YCoordinate         DECIMAL(6,2) NULL,
    DistanceFeet        DECIMAL(7,2) NULL,
    AngleDegrees        DECIMAL(7,2) NULL,
    IsGoal              BIT NOT NULL DEFAULT 0,
    IsBlocked           BIT NOT NULL DEFAULT 0,
    IsMissed            BIT NOT NULL DEFAULT 0,
    StrengthCode        VARCHAR(20) NULL,       -- raw NHL situationCode as ingested; see Reference.SituationCodeMap
    HomeScore           SMALLINT NULL,
    AwayScore           SMALLINT NULL,
    CreatedAt           DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_Shots PRIMARY KEY (ShotID),
    CONSTRAINT UQ_Shots_Play UNIQUE (PlayID),
    CONSTRAINT FK_Shots_Play FOREIGN KEY (PlayID)
        REFERENCES Game.Plays(PlayID),
    CONSTRAINT FK_Shots_Game FOREIGN KEY (GameID)
        REFERENCES Game.Games(GameID),
    CONSTRAINT FK_Shots_Team FOREIGN KEY (TeamID)
        REFERENCES Reference.Teams(TeamID),
    CONSTRAINT FK_Shots_Shooter FOREIGN KEY (ShooterPlayerID)
        REFERENCES Reference.Players(PlayerID),
    CONSTRAINT FK_Shots_Goalie FOREIGN KEY (GoaliePlayerID)
        REFERENCES Reference.Players(PlayerID)
);
GO

CREATE TABLE Game.Goals
(
    GoalID              BIGINT IDENTITY(1,1) NOT NULL,
    ShotID              BIGINT NOT NULL,
    PlayID              BIGINT NOT NULL,
    GameID              BIGINT NOT NULL,
    TeamID              INT NOT NULL,
    ScoringPlayerID     BIGINT NOT NULL,
    Assist1PlayerID     BIGINT NULL,
    Assist2PlayerID     BIGINT NULL,
    PeriodNumber        TINYINT NOT NULL,
    PeriodTimeSeconds   SMALLINT NULL,
    GoalType             VARCHAR(50) NULL,
    StrengthCode         VARCHAR(20) NULL,      -- raw NHL situationCode as ingested; see Reference.SituationCodeMap
    IsPowerPlay          BIT NOT NULL DEFAULT 0,
    IsShortHanded        BIT NOT NULL DEFAULT 0,
    IsEmptyNet           BIT NOT NULL DEFAULT 0,
    HomeScore             SMALLINT NULL,
    AwayScore             SMALLINT NULL,
    HighlightClipID       BIGINT NULL,            -- Brightcove video id (account 6415718365001); resolve to a
    DiscreteClipID        BIGINT NULL,            -- playable URL on demand via the Playback API -- the resolved
    ClipSharingURL         VARCHAR(500) NULL,      -- URL expires in ~1hr, so only the stable id is stored long-term
    CONSTRAINT PK_Goals PRIMARY KEY (GoalID),
    CONSTRAINT UQ_Goals_Shot UNIQUE (ShotID),
    CONSTRAINT FK_Goals_Shot FOREIGN KEY (ShotID)
        REFERENCES Game.Shots(ShotID),
    CONSTRAINT FK_Goals_Play FOREIGN KEY (PlayID)
        REFERENCES Game.Plays(PlayID),
    CONSTRAINT FK_Goals_Game FOREIGN KEY (GameID)
        REFERENCES Game.Games(GameID),
    CONSTRAINT FK_Goals_Team FOREIGN KEY (TeamID)
        REFERENCES Reference.Teams(TeamID),
    CONSTRAINT FK_Goals_Scorer FOREIGN KEY (ScoringPlayerID)
        REFERENCES Reference.Players(PlayerID),
    CONSTRAINT FK_Goals_Assist1 FOREIGN KEY (Assist1PlayerID)
        REFERENCES Reference.Players(PlayerID),
    CONSTRAINT FK_Goals_Assist2 FOREIGN KEY (Assist2PlayerID)
        REFERENCES Reference.Players(PlayerID)
);
GO

-- Ingested directly from the shift-charts endpoint (source of truth for TOI). See gap #6.
CREATE TABLE Game.Shifts
(
    ShiftID             BIGINT IDENTITY(1,1) NOT NULL,
    GameID              BIGINT NOT NULL,
    PlayerID            BIGINT NOT NULL,
    TeamID              INT NOT NULL,
    PeriodNumber        TINYINT NOT NULL,
    ShiftStartSeconds   SMALLINT NOT NULL,
    ShiftEndSeconds     SMALLINT NOT NULL,
    DurationSeconds     SMALLINT NOT NULL,
    CONSTRAINT PK_Shifts PRIMARY KEY (ShiftID),
    CONSTRAINT UQ_Shifts UNIQUE (GameID, PlayerID, PeriodNumber, ShiftStartSeconds),
    CONSTRAINT FK_Shifts_Game FOREIGN KEY (GameID)
        REFERENCES Game.Games(GameID),
    CONSTRAINT FK_Shifts_Player FOREIGN KEY (PlayerID)
        REFERENCES Reference.Players(PlayerID),
    CONSTRAINT FK_Shifts_Team FOREIGN KEY (TeamID)
        REFERENCES Reference.Teams(TeamID)
);
GO

-- Derived during ingestion (NOT separately ingested): for each play, join Game.Shifts on
-- PeriodNumber + time range to find who was on the ice at that instant. See gap #6.
CREATE TABLE Game.PlayOnIcePlayers
(
    PlayOnIcePlayerID  BIGINT IDENTITY(1,1) NOT NULL,
    PlayID             BIGINT NOT NULL,
    PlayerID           BIGINT NOT NULL,
    TeamID             INT NOT NULL,
    RoleCode           VARCHAR(20) NOT NULL,
    CONSTRAINT PK_PlayOnIcePlayers PRIMARY KEY (PlayOnIcePlayerID),
    CONSTRAINT UQ_PlayOnIcePlayers UNIQUE (PlayID, PlayerID),
    CONSTRAINT FK_POIP_Play FOREIGN KEY (PlayID)
        REFERENCES Game.Plays(PlayID),
    CONSTRAINT FK_POIP_Player FOREIGN KEY (PlayerID)
        REFERENCES Reference.Players(PlayerID),
    CONSTRAINT FK_POIP_Team FOREIGN KEY (TeamID)
        REFERENCES Reference.Teams(TeamID)
);
GO

-- ============================================================================
-- 3. Stats schema (official NHL statistics)
-- ============================================================================

CREATE TABLE Stats.PlayerGameStats
(
    PlayerGameStatsID       BIGINT IDENTITY(1,1) NOT NULL,
    GameID                  BIGINT NOT NULL,
    PlayerID                BIGINT NOT NULL,
    TeamID                  INT NOT NULL,
    PositionCode            VARCHAR(5) NULL,
    Goals                   SMALLINT NULL,
    Assists                 SMALLINT NULL,
    Points                  SMALLINT NULL,
    Shots                   SMALLINT NULL,
    Hits                    SMALLINT NULL,
    Blocks                  SMALLINT NULL,
    Giveaways               SMALLINT NULL,
    Takeaways               SMALLINT NULL,
    PenaltyMinutes          SMALLINT NULL,
    FaceoffWins             SMALLINT NULL,
    FaceoffLosses           SMALLINT NULL,
    TimeOnIceSeconds        INT NULL,
    PowerPlayTOISeconds     INT NULL,
    ShortHandedTOISeconds   INT NULL,
    PowerPlayGoals          SMALLINT NULL,
    PowerPlayAssists        SMALLINT NULL,
    ShortHandedGoals        SMALLINT NULL,
    ShortHandedAssists      SMALLINT NULL,
    CreatedAt               DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_PlayerGameStats PRIMARY KEY (PlayerGameStatsID),
    CONSTRAINT UQ_PlayerGameStats UNIQUE (GameID, PlayerID, TeamID),
    CONSTRAINT FK_PGS_Game FOREIGN KEY (GameID) REFERENCES Game.Games(GameID),
    CONSTRAINT FK_PGS_Player FOREIGN KEY (PlayerID) REFERENCES Reference.Players(PlayerID),
    CONSTRAINT FK_PGS_Team FOREIGN KEY (TeamID) REFERENCES Reference.Teams(TeamID)
);
GO

CREATE TABLE Stats.PlayerSeasonStats
(
    PlayerSeasonStatsID     BIGINT IDENTITY(1,1) NOT NULL,
    SeasonID                INT NOT NULL,
    PlayerID                BIGINT NOT NULL,
    TeamID                  INT NOT NULL,
    GamesPlayed             SMALLINT NULL,
    Goals                   INT NULL,
    Assists                 INT NULL,
    Points                  INT NULL,
    Shots                   INT NULL,
    Hits                    INT NULL,
    Blocks                  INT NULL,
    Giveaways               INT NULL,
    Takeaways               INT NULL,
    PenaltyMinutes          INT NULL,
    FaceoffWins             INT NULL,
    FaceoffLosses           INT NULL,
    TimeOnIceSeconds        BIGINT NULL,
    PowerPlayTOISeconds     BIGINT NULL,
    ShortHandedTOISeconds   BIGINT NULL,
    PowerPlayGoals          INT NULL,
    PowerPlayAssists        INT NULL,
    ShortHandedGoals        INT NULL,
    ShortHandedAssists      INT NULL,
    AverageTOIMinutes       AS (CAST(TimeOnIceSeconds AS DECIMAL(18,4)) / 60.0 / NULLIF(GamesPlayed, 0)) PERSISTED,
    AveragePowerPlayTOIMinutes AS (CAST(PowerPlayTOISeconds AS DECIMAL(18,4)) / 60.0 / NULLIF(GamesPlayed, 0)) PERSISTED,
    AverageShortHandedTOIMinutes AS (CAST(ShortHandedTOISeconds AS DECIMAL(18,4)) / 60.0 / NULLIF(GamesPlayed, 0)) PERSISTED,
    CreatedAt               DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_PlayerSeasonStats PRIMARY KEY (PlayerSeasonStatsID),
    CONSTRAINT UQ_PlayerSeasonStats UNIQUE (SeasonID, PlayerID, TeamID),
    CONSTRAINT FK_PSS_Season FOREIGN KEY (SeasonID) REFERENCES Reference.Seasons(SeasonID),
    CONSTRAINT FK_PSS_Player FOREIGN KEY (PlayerID) REFERENCES Reference.Players(PlayerID),
    CONSTRAINT FK_PSS_Team FOREIGN KEY (TeamID) REFERENCES Reference.Teams(TeamID)
);
GO

-- ============================================================================
-- 4. Analytics schema
-- ============================================================================

CREATE TABLE Analytics.CalculationVersions
(
    CalculationVersionID    INT IDENTITY(1,1) NOT NULL,
    MetricCode              VARCHAR(50) NOT NULL,
    VersionName             VARCHAR(100) NOT NULL,
    ModelDescription        VARCHAR(MAX) NULL,
    CreatedAt               DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
    IsActive                BIT NOT NULL DEFAULT 1,
    CONSTRAINT PK_CalculationVersions PRIMARY KEY (CalculationVersionID),
    CONSTRAINT UQ_CalculationVersions UNIQUE (MetricCode, VersionName)
);
GO

-- Resolves gap #3: at most one active version per metric, so ingestion can always find
-- "the configured model" via WHERE MetricCode = ? AND IsActive = 1.
CREATE UNIQUE INDEX UQ_CalculationVersions_ActivePerMetric
    ON Analytics.CalculationVersions(MetricCode)
    WHERE IsActive = 1;
GO

CREATE TABLE Analytics.ShotExpectedGoals
(
    ShotExpectedGoalsID     BIGINT IDENTITY(1,1) NOT NULL,
    ShotID                  BIGINT NOT NULL,
    CalculationVersionID    INT NOT NULL,
    ExpectedGoals           DECIMAL(12,8) NOT NULL,
    CalculatedAt            DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_ShotExpectedGoals PRIMARY KEY (ShotExpectedGoalsID),
    CONSTRAINT UQ_ShotExpectedGoals UNIQUE (ShotID, CalculationVersionID),
    CONSTRAINT FK_ShotXG_Shot FOREIGN KEY (ShotID)
        REFERENCES Game.Shots(ShotID),
    CONSTRAINT FK_ShotXG_Version FOREIGN KEY (CalculationVersionID)
        REFERENCES Analytics.CalculationVersions(CalculationVersionID)
);
GO

CREATE TABLE Analytics.PlayerGameAdvancedStats
(
    PlayerGameAdvancedStatsID      BIGINT IDENTITY(1,1) NOT NULL,
    GameID                         BIGINT NOT NULL,
    PlayerID                       BIGINT NOT NULL,
    TeamID                         INT NOT NULL,
    SituationID                    INT NOT NULL,
    CalculationVersionID           INT NOT NULL,

    IndividualCorsiFor             INT NULL,
    IndividualFenwickFor           INT NULL,
    IndividualShots                INT NULL,
    IndividualGoals                INT NULL,
    IndividualExpectedGoals        DECIMAL(12,4) NULL,
    IndividualScoringChances       INT NULL,
    IndividualHighDangerChances    INT NULL,

    IndividualCorsiForPct          DECIMAL(7,4) NULL,
    ShootingPercentage             DECIMAL(7,4) NULL,

    CalculatedAt                   DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME(),

    CONSTRAINT PK_PlayerGameAdvancedStats PRIMARY KEY (PlayerGameAdvancedStatsID),
    CONSTRAINT UQ_PlayerGameAdvancedStats
        UNIQUE (GameID, PlayerID, TeamID, SituationID, CalculationVersionID),

    CONSTRAINT FK_PGAS_Game FOREIGN KEY (GameID)
        REFERENCES Game.Games(GameID),
    CONSTRAINT FK_PGAS_Player FOREIGN KEY (PlayerID)
        REFERENCES Reference.Players(PlayerID),
    CONSTRAINT FK_PGAS_Team FOREIGN KEY (TeamID)
        REFERENCES Reference.Teams(TeamID),
    CONSTRAINT FK_PGAS_Situation FOREIGN KEY (SituationID)
        REFERENCES Reference.Situations(SituationID),
    CONSTRAINT FK_PGAS_Version FOREIGN KEY (CalculationVersionID)
        REFERENCES Analytics.CalculationVersions(CalculationVersionID)
);
GO

CREATE TABLE Analytics.PlayerGameOnIceStats
(
    PlayerGameOnIceStatsID     BIGINT IDENTITY(1,1) NOT NULL,
    GameID                     BIGINT NOT NULL,
    PlayerID                   BIGINT NOT NULL,
    TeamID                     INT NOT NULL,
    SituationID                INT NOT NULL,
    CalculationVersionID       INT NOT NULL,

    TimeOnIceSeconds           INT NULL,

    CorsiFor                   INT NULL,
    CorsiAgainst                INT NULL,
    CorsiForPct                 DECIMAL(7,4) NULL,

    FenwickFor                  INT NULL,
    FenwickAgainst               INT NULL,
    FenwickForPct                DECIMAL(7,4) NULL,

    ShotsFor                    INT NULL,
    ShotsAgainst                 INT NULL,

    GoalsFor                    INT NULL,
    GoalsAgainst                 INT NULL,

    ExpectedGoalsFor             DECIMAL(12,4) NULL,
    ExpectedGoalsAgainst         DECIMAL(12,4) NULL,
    ExpectedGoalsPct             DECIMAL(7,4) NULL,

    ScoringChancesFor            INT NULL,
    ScoringChancesAgainst        INT NULL,
    HighDangerChancesFor         INT NULL,
    HighDangerChancesAgainst     INT NULL,

    PDO                          DECIMAL(8,4) NULL,
    OnIceShootingPct             DECIMAL(8,4) NULL,
    OnIceSavePct                 DECIMAL(8,4) NULL,

    CalculatedAt                 DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME(),

    CONSTRAINT PK_PlayerGameOnIceStats PRIMARY KEY (PlayerGameOnIceStatsID),
    CONSTRAINT UQ_PlayerGameOnIceStats
        UNIQUE (GameID, PlayerID, TeamID, SituationID, CalculationVersionID),

    CONSTRAINT FK_PGOIS_Game FOREIGN KEY (GameID)
        REFERENCES Game.Games(GameID),
    CONSTRAINT FK_PGOIS_Player FOREIGN KEY (PlayerID)
        REFERENCES Reference.Players(PlayerID),
    CONSTRAINT FK_PGOIS_Team FOREIGN KEY (TeamID)
        REFERENCES Reference.Teams(TeamID),
    CONSTRAINT FK_PGOIS_Situation FOREIGN KEY (SituationID)
        REFERENCES Reference.Situations(SituationID),
    CONSTRAINT FK_PGOIS_Version FOREIGN KEY (CalculationVersionID)
        REFERENCES Analytics.CalculationVersions(CalculationVersionID)
);
GO

CREATE TABLE Analytics.GoalieGameAdvancedStats
(
    GoalieGameAdvancedStatsID      BIGINT IDENTITY(1,1) NOT NULL,
    GameID                          BIGINT NOT NULL,
    GoaliePlayerID                  BIGINT NOT NULL,
    TeamID                          INT NOT NULL,
    SituationID                     INT NOT NULL,
    CalculationVersionID            INT NOT NULL,

    ShotsAgainst                    INT NULL,
    Saves                           INT NULL,
    GoalsAgainst                    INT NULL,
    SavePercentage                  DECIMAL(8,5) NULL,

    ExpectedGoalsAgainst            DECIMAL(12,4) NULL,
    GoalsSavedAboveExpected         DECIMAL(12,4) NULL,
    ExpectedSavePercentage          DECIMAL(8,5) NULL,

    HighDangerShotsAgainst          INT NULL,
    HighDangerSaves                 INT NULL,
    HighDangerGoalsAgainst          INT NULL,

    ReboundShotsAgainst             INT NULL,
    ReboundGoalsAgainst             INT NULL,
    RushShotsAgainst                INT NULL,
    RushGoalsAgainst                INT NULL,

    CalculatedAt                    DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME(),

    CONSTRAINT PK_GoalieGameAdvancedStats PRIMARY KEY (GoalieGameAdvancedStatsID),
    CONSTRAINT UQ_GoalieGameAdvancedStats
        UNIQUE (GameID, GoaliePlayerID, TeamID, SituationID, CalculationVersionID),

    CONSTRAINT FK_GGAS_Game FOREIGN KEY (GameID)
        REFERENCES Game.Games(GameID),
    CONSTRAINT FK_GGAS_Goalie FOREIGN KEY (GoaliePlayerID)
        REFERENCES Reference.Players(PlayerID),
    CONSTRAINT FK_GGAS_Team FOREIGN KEY (TeamID)
        REFERENCES Reference.Teams(TeamID),
    CONSTRAINT FK_GGAS_Situation FOREIGN KEY (SituationID)
        REFERENCES Reference.Situations(SituationID),
    CONSTRAINT FK_GGAS_Version FOREIGN KEY (CalculationVersionID)
        REFERENCES Analytics.CalculationVersions(CalculationVersionID)
);
GO

-- ============================================================================
-- 5. Ingestion schema (resolves gap #1 — not defined anywhere in the report)
-- ============================================================================

-- One row per (game, endpoint); upserted on re-ingestion rather than kept as history.
CREATE TABLE Ingestion.RawApiResponses
(
    RawApiResponseID    BIGINT IDENTITY(1,1) NOT NULL,
    GameID               BIGINT NOT NULL,
    EndpointType         VARCHAR(50) NOT NULL,   -- e.g. 'PLAY_BY_PLAY', 'SHIFT_CHARTS', 'BOXSCORE'
    RawJSON              NVARCHAR(MAX) NOT NULL,
    RetrievedAt          DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_RawApiResponses PRIMARY KEY (RawApiResponseID),
    CONSTRAINT UQ_RawApiResponses UNIQUE (GameID, EndpointType),
    CONSTRAINT FK_RawApiResponses_Game FOREIGN KEY (GameID)
        REFERENCES Game.Games(GameID)
);
GO

-- Append-only log of each ingestion/recalculation workflow stage (see report section 13).
CREATE TABLE Ingestion.IngestionRuns
(
    IngestionRunID   BIGINT IDENTITY(1,1) NOT NULL,
    GameID            BIGINT NULL,
    SeasonID          INT NULL,
    Stage             VARCHAR(50) NOT NULL,      -- e.g. 'PLAY_BY_PLAY_PARSE', 'SHIFT_PARSE', 'XG_CALC'
    Status            VARCHAR(20) NOT NULL,      -- 'SUCCESS', 'FAILED', 'IN_PROGRESS'
    ErrorMessage      VARCHAR(MAX) NULL,
    StartedAt         DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
    CompletedAt       DATETIME2(0) NULL,
    CONSTRAINT PK_IngestionRuns PRIMARY KEY (IngestionRunID),
    CONSTRAINT FK_IngestionRuns_Game FOREIGN KEY (GameID)
        REFERENCES Game.Games(GameID),
    CONSTRAINT FK_IngestionRuns_Season FOREIGN KEY (SeasonID)
        REFERENCES Reference.Seasons(SeasonID)
);
GO

-- ============================================================================
-- 6. Projections schema
-- ============================================================================

CREATE TABLE Projections.Sources
(
    SourceID        INT IDENTITY(1,1) NOT NULL,
    SourceName      VARCHAR(100) NOT NULL,     -- e.g. 'DtZ'
    SeasonID        INT NOT NULL,
    Description     VARCHAR(500) NULL,
    ImportedAt      DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_Sources PRIMARY KEY (SourceID),
    CONSTRAINT UQ_Sources_NameSeason UNIQUE (SourceName, SeasonID),
    CONSTRAINT FK_Sources_Season FOREIGN KEY (SeasonID)
        REFERENCES Reference.Seasons(SeasonID)
);
GO

-- Crosswalk from a projection sheet's raw player-name spelling to our PlayerID -- sheets
-- often spell a name differently than the NHL API (suffixes, accents, nicknames). Resolved
-- once per (SourceID, RawName) and reused on every re-import of that source.
CREATE TABLE Projections.PlayerNameAliases
(
    PlayerNameAliasID  BIGINT IDENTITY(1,1) NOT NULL,
    SourceID            INT NOT NULL,
    RawName             VARCHAR(200) NOT NULL,
    PlayerID            BIGINT NOT NULL,
    CreatedAt           DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_PlayerNameAliases PRIMARY KEY (PlayerNameAliasID),
    CONSTRAINT UQ_PlayerNameAliases_SourceRaw UNIQUE (SourceID, RawName),
    CONSTRAINT FK_PNA_Source FOREIGN KEY (SourceID)
        REFERENCES Projections.Sources(SourceID),
    CONSTRAINT FK_PNA_Player FOREIGN KEY (PlayerID)
        REFERENCES Reference.Players(PlayerID)
);
GO

-- A raw sheet name that didn't resolve to exactly one player via PlayerNameAliases /
-- normalized-name matching. The import that hit it skips just this row (the rest of the
-- sheet still loads) and records it here for one-time manual review; resolving it means
-- adding the correct row to PlayerNameAliases and re-running the import for that source.
CREATE TABLE Projections.UnresolvedPlayerNames
(
    UnresolvedPlayerNameID  BIGINT IDENTITY(1,1) NOT NULL,
    SourceID                 INT NOT NULL,
    RawName                  VARCHAR(200) NOT NULL,
    CandidatePlayerIDs       VARCHAR(200) NULL,     -- comma-separated PlayerIDs when the name matched more than one player; NULL when it matched none
    FirstSeenAt               DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_UnresolvedPlayerNames PRIMARY KEY (UnresolvedPlayerNameID),
    CONSTRAINT UQ_UnresolvedPlayerNames_SourceRaw UNIQUE (SourceID, RawName),
    CONSTRAINT FK_UPN_Source FOREIGN KEY (SourceID)
        REFERENCES Projections.Sources(SourceID)
);
GO

-- Split from goalies (below): skater and goalie projections don't share stat categories.
CREATE TABLE Projections.SkaterProjections
(
    SkaterProjectionID   BIGINT IDENTITY(1,1) NOT NULL,
    SourceID              INT NOT NULL,
    PlayerID              BIGINT NOT NULL,
    TeamID                INT NULL,
    GamesPlayed           SMALLINT NULL,
    Goals                 INT NULL,
    Assists               INT NULL,
    Points                INT NULL,
    PowerPlayPoints       INT NULL,
    ShortHandedPoints     INT NULL,
    Shots                 INT NULL,
    Hits                  INT NULL,
    Blocks                INT NULL,
    PenaltyMinutes        INT NULL,
    AverageTOIMinutes     DECIMAL(5,2) NULL,
    FaceoffWinPct         DECIMAL(5,4) NULL,
    PlusMinus             INT NULL,
    PowerPlayGoals        INT NULL,
    PowerPlayAssists      INT NULL,
    FaceoffWins           INT NULL,
    FaceoffLosses         INT NULL,
    CreatedAt             DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_SkaterProjections PRIMARY KEY (SkaterProjectionID),
    CONSTRAINT UQ_SkaterProjections_SourcePlayer UNIQUE (SourceID, PlayerID),
    CONSTRAINT FK_SkP_Source FOREIGN KEY (SourceID)
        REFERENCES Projections.Sources(SourceID),
    CONSTRAINT FK_SkP_Player FOREIGN KEY (PlayerID)
        REFERENCES Reference.Players(PlayerID),
    CONSTRAINT FK_SkP_Team FOREIGN KEY (TeamID)
        REFERENCES Reference.Teams(TeamID)
);
GO

CREATE TABLE Projections.GoalieProjections
(
    GoalieProjectionID    BIGINT IDENTITY(1,1) NOT NULL,
    SourceID               INT NOT NULL,
    PlayerID               BIGINT NOT NULL,
    TeamID                 INT NULL,
    GamesPlayed            SMALLINT NULL,
    Wins                   SMALLINT NULL,
    Losses                 SMALLINT NULL,
    OvertimeLosses         SMALLINT NULL,
    GoalsAgainstAverage    DECIMAL(5,3) NULL,
    SavePercentage         DECIMAL(6,4) NULL,
    Shutouts               SMALLINT NULL,
    GamesStarted           SMALLINT NULL,
    GoalsAgainst           INT NULL,
    ShotsAgainst           INT NULL,
    Saves                  INT NULL,
    CreatedAt              DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_GoalieProjections PRIMARY KEY (GoalieProjectionID),
    CONSTRAINT UQ_GoalieProjections_SourcePlayer UNIQUE (SourceID, PlayerID),
    CONSTRAINT FK_GP_Source FOREIGN KEY (SourceID)
        REFERENCES Projections.Sources(SourceID),
    CONSTRAINT FK_GP_Player FOREIGN KEY (PlayerID)
        REFERENCES Reference.Players(PlayerID),
    CONSTRAINT FK_GP_Team FOREIGN KEY (TeamID)
        REFERENCES Reference.Teams(TeamID)
);
GO

-- ============================================================================
-- 7. Fantasy schema
-- ============================================================================

-- Yahoo/ESPN/Fantrax/etc. Kept distinct from Projections.Sources -- a platform is where you
-- draft/manage a team (positions, ADP), a projections source is an opinion about future
-- stats; a sheet like Fantrax happens to publish both, but the two are independent axes.
CREATE TABLE Fantasy.Platforms
(
    FantasyPlatformID   INT IDENTITY(1,1) NOT NULL,
    PlatformName        VARCHAR(50) NOT NULL,
    CreatedAt           DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_Platforms PRIMARY KEY (FantasyPlatformID),
    CONSTRAINT UQ_Platforms_Name UNIQUE (PlatformName)
);
GO

-- A platform's fantasy-position eligibility for a player, which is its own per-platform,
-- per-season concept distinct from Reference.Players.PositionCode (the player's real on-ice
-- position) -- it can differ from it, include more than one position, and change mid-season
-- as a player accrues starts at a new position. One row per eligible position, not a
-- delimited list, so a player with multiple eligible positions is just multiple rows.
CREATE TABLE Fantasy.PlayerPositions
(
    FantasyPlayerPositionID  BIGINT IDENTITY(1,1) NOT NULL,
    FantasyPlatformID         INT NOT NULL,
    PlayerID                  BIGINT NOT NULL,
    SeasonID                  INT NOT NULL,
    PositionCode               VARCHAR(10) NOT NULL,
    CreatedAt                  DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_PlayerPositions PRIMARY KEY (FantasyPlayerPositionID),
    CONSTRAINT UQ_PlayerPositions UNIQUE (FantasyPlatformID, PlayerID, SeasonID, PositionCode),
    CONSTRAINT FK_FPP_Platform FOREIGN KEY (FantasyPlatformID)
        REFERENCES Fantasy.Platforms(FantasyPlatformID),
    CONSTRAINT FK_FPP_Player FOREIGN KEY (PlayerID)
        REFERENCES Reference.Players(PlayerID),
    CONSTRAINT FK_FPP_Season FOREIGN KEY (SeasonID)
        REFERENCES Reference.Seasons(SeasonID)
);
GO

-- A platform's current average draft position for a player. One row per (platform, player,
-- season), upserted as fresh ADP data comes in -- same "current snapshot, not a history
-- table" approach as Projections.SkaterProjections/GoalieProjections, since ADP drifts
-- continuously through the draft season rather than being a single fixed value.
CREATE TABLE Fantasy.PlayerADP
(
    FantasyPlayerADPID   BIGINT IDENTITY(1,1) NOT NULL,
    FantasyPlatformID     INT NOT NULL,
    PlayerID               BIGINT NOT NULL,
    SeasonID                INT NOT NULL,
    ADP                     DECIMAL(6,2) NOT NULL,
    UpdatedAt               DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_PlayerADP PRIMARY KEY (FantasyPlayerADPID),
    CONSTRAINT UQ_PlayerADP UNIQUE (FantasyPlatformID, PlayerID, SeasonID),
    CONSTRAINT FK_FPA_Platform FOREIGN KEY (FantasyPlatformID)
        REFERENCES Fantasy.Platforms(FantasyPlatformID),
    CONSTRAINT FK_FPA_Player FOREIGN KEY (PlayerID)
        REFERENCES Reference.Players(PlayerID),
    CONSTRAINT FK_FPA_Season FOREIGN KEY (SeasonID)
        REFERENCES Reference.Seasons(SeasonID)
);
GO

-- Same name-resolution problem as Projections.PlayerNameAliases (a platform's own player-name
-- spelling vs. ours), reused by the shared nhl_pipeline.name_resolver module -- hence the
-- column name "SourceID" here too (matching Projections.Sources.SourceID) even though it
-- references Fantasy.Platforms: the resolver's SQL is schema-agnostic and expects that name.
CREATE TABLE Fantasy.PlayerNameAliases
(
    PlayerNameAliasID  BIGINT IDENTITY(1,1) NOT NULL,
    SourceID            INT NOT NULL,
    RawName             VARCHAR(200) NOT NULL,
    PlayerID            BIGINT NOT NULL,
    CreatedAt           DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_FantasyPlayerNameAliases PRIMARY KEY (PlayerNameAliasID),
    CONSTRAINT UQ_FantasyPlayerNameAliases_SourceRaw UNIQUE (SourceID, RawName),
    CONSTRAINT FK_FNA_Platform FOREIGN KEY (SourceID)
        REFERENCES Fantasy.Platforms(FantasyPlatformID),
    CONSTRAINT FK_FNA_Player FOREIGN KEY (PlayerID)
        REFERENCES Reference.Players(PlayerID)
);
GO

CREATE TABLE Fantasy.UnresolvedPlayerNames
(
    UnresolvedPlayerNameID  BIGINT IDENTITY(1,1) NOT NULL,
    SourceID                 INT NOT NULL,
    RawName                  VARCHAR(200) NOT NULL,
    CandidatePlayerIDs       VARCHAR(200) NULL,
    FirstSeenAt               DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_FantasyUnresolvedPlayerNames PRIMARY KEY (UnresolvedPlayerNameID),
    CONSTRAINT UQ_FantasyUnresolvedPlayerNames_SourceRaw UNIQUE (SourceID, RawName),
    CONSTRAINT FK_FUPN_Platform FOREIGN KEY (SourceID)
        REFERENCES Fantasy.Platforms(FantasyPlatformID)
);
GO

-- ============================================================================
-- 8. Indexes
-- ============================================================================

CREATE INDEX IX_Plays_GameID
    ON Game.Plays(GameID);

CREATE INDEX IX_Shots_GameID
    ON Game.Shots(GameID);

CREATE INDEX IX_Shots_TeamID
    ON Game.Shots(TeamID);

CREATE INDEX IX_Shots_Shooter
    ON Game.Shots(ShooterPlayerID);

CREATE INDEX IX_Shots_Goalie
    ON Game.Shots(GoaliePlayerID);

CREATE INDEX IX_Goals_ScoringPlayer
    ON Game.Goals(ScoringPlayerID);

CREATE INDEX IX_Shifts_PlayerGame
    ON Game.Shifts(PlayerID, GameID);

CREATE INDEX IX_PlayOnIcePlayers_Play
    ON Game.PlayOnIcePlayers(PlayID);

CREATE INDEX IX_PlayOnIcePlayers_Player
    ON Game.PlayOnIcePlayers(PlayerID);

CREATE INDEX IX_PlayerGameStats_Player
    ON Stats.PlayerGameStats(PlayerID);

CREATE INDEX IX_PlayerSeasonStats_Player
    ON Stats.PlayerSeasonStats(PlayerID);

CREATE INDEX IX_PlayerGameAdvanced_Player
    ON Analytics.PlayerGameAdvancedStats(PlayerID);

CREATE INDEX IX_PlayerGameOnIce_Player
    ON Analytics.PlayerGameOnIceStats(PlayerID);

CREATE INDEX IX_GoalieGameAdvanced_Goalie
    ON Analytics.GoalieGameAdvancedStats(GoaliePlayerID);

CREATE INDEX IX_IngestionRuns_GameStage
    ON Ingestion.IngestionRuns(GameID, Stage);

CREATE INDEX IX_PlayerNameAliases_Player
    ON Projections.PlayerNameAliases(PlayerID);

CREATE INDEX IX_SkaterProjections_Player
    ON Projections.SkaterProjections(PlayerID);

CREATE INDEX IX_GoalieProjections_Player
    ON Projections.GoalieProjections(PlayerID);

CREATE INDEX IX_Schedule_SeasonDate
    ON Reference.Schedule(SeasonID, GameDate);

CREATE INDEX IX_Schedule_HomeTeam
    ON Reference.Schedule(HomeTeamID);

CREATE INDEX IX_Schedule_AwayTeam
    ON Reference.Schedule(AwayTeamID);

CREATE INDEX IX_PlayerPositions_Player
    ON Fantasy.PlayerPositions(PlayerID);

CREATE INDEX IX_PlayerADP_Player
    ON Fantasy.PlayerADP(PlayerID);

CREATE INDEX IX_FantasyPlayerNameAliases_Player
    ON Fantasy.PlayerNameAliases(PlayerID);
GO

-- ============================================================================
-- 9. Seed data
-- ============================================================================

-- Situations. "ALL" is the aggregate row (no strength filter) used when a metric is
-- calculated across every situation rather than split out by strength state.
INSERT INTO Reference.Situations (SituationCode, Description, StrengthHome, StrengthAway, IncludeEmptyNet)
VALUES
    ('ALL', 'All situations combined',            NULL, NULL, 1),
    ('5V5', 'Even strength 5v5',                    5,    5,   1),
    ('5V4', 'Power play 5v4',                       5,    4,   1),
    ('4V5', 'Penalty kill 4v5',                      4,    5,   1),
    ('4V4', '4v4',                                   4,    4,   1),
    ('3V3', '3v3 (overtime)',                        3,    3,   1),
    ('5V3', 'Power play 5v3',                        5,    3,   1),
    ('3V5', 'Penalty kill 3v5',                      3,    5,   1),
    ('6V5', 'Power play, extra attacker (empty net for)',   6, 5, 1),
    ('5V6', 'Penalty kill, opponent extra attacker (empty net against)', 5, 6, 1),
    ('4V3', 'Power play 4v3 (double minor)', 4, 3, 1),
    ('3V4', 'Penalty kill 3v4 (double minor)', 3, 4, 1),
    ('6V3', 'Power play 6v3, extra attacker (empty net for, double minor)', 6, 3, 1),
    ('3V6', 'Penalty kill 3v6, opponent extra attacker (empty net against, double minor)', 3, 6, 1),
    ('6V4', 'Power play 6v4, extra attacker (empty net for)', 6, 4, 1),
    ('4V6', 'Penalty kill 4v6, opponent extra attacker (empty net against)', 4, 6, 1);
GO

-- SituationCodeMap. NHL API situationCode is 4 chars: [awayGoalieInNet][awaySkaters]
-- [homeSkaters][homeGoalieInNet]. Starter set plus the additional codes found by running
-- a full 2025-26 regular-season backfill (1312 games) and mining Ingestion.IngestionRuns
-- for every SITUATION_RESOLVE_UNMAPPED warning — extend the same way as new codes surface.
-- Two rare codes ('0101', '1010'; ~12 and ~21 shots out of 152,957 all season) parse to a
-- team having 0 skaters under this scheme, which isn't physically possible, and were left
-- unmapped deliberately rather than guessed at — they fall back to the 'ALL' bucket.
INSERT INTO Reference.SituationCodeMap (RawSituationCode, AwayGoalieInNet, AwaySkaters, HomeSkaters, HomeGoalieInNet)
VALUES
    ('1551', 1, 5, 5, 1),  -- 5v5
    ('1541', 1, 5, 4, 1),  -- away 5, home 4
    ('1451', 1, 4, 5, 1),  -- away 4, home 5
    ('1441', 1, 4, 4, 1),  -- 4v4
    ('1331', 1, 3, 3, 1),  -- 3v3
    ('1531', 1, 5, 3, 1),  -- away 5, home 3
    ('1351', 1, 3, 5, 1),  -- away 3, home 5
    ('0541', 0, 5, 4, 1),  -- away goalie pulled, away 5, home 4
    ('1540', 1, 5, 4, 0),  -- home goalie pulled, away 5, home 4
    ('0431', 0, 4, 3, 1),  -- away pulled goalie during a 5-on-3-style advantage -> away 4v3
    ('0551', 0, 5, 5, 1),  -- away pulled goalie at even strength -> resolves to 5V5
    ('0631', 0, 6, 3, 1),  -- away pulled goalie during a 5-on-3 -> away 6v3
    ('0641', 0, 6, 4, 1),  -- away pulled goalie during a power play -> away 6v4
    ('0651', 0, 6, 5, 1),  -- away pulled goalie, even strength -> away 6v5
    ('1340', 1, 3, 4, 0),  -- home pulled goalie while shorthanded 2 men -> home 4v3
    ('1341', 1, 3, 4, 1),  -- away shorthanded 2 men -> home 4v3
    ('1431', 1, 4, 3, 1),  -- away power play -> away 4v3
    ('1450', 1, 4, 5, 0),  -- home pulled goalie while shorthanded -> home 5v4
    ('1460', 1, 4, 6, 0),  -- home pulled goalie during a power play -> home 6v4
    ('1550', 1, 5, 5, 0),  -- home pulled goalie at even strength -> resolves to 5V5
    ('1560', 1, 5, 6, 0);  -- home pulled goalie, even strength -> home 6v5
GO

-- CalculationVersions. xG and GSAx start as naive/placeholder models per V1 scope; Corsi
-- and Fenwick use standard definitions since those are simple counting stats, not models.
INSERT INTO Analytics.CalculationVersions (MetricCode, VersionName, ModelDescription, IsActive)
VALUES
    ('XG', 'naive_v1',
        'Placeholder expected-goals model: flat probability looked up by shot type and distance bucket only. Not fit to historical data — replace before relying on it for real analysis.',
        1),
    ('CORSI', 'standard_v1',
        'Standard Corsi definition: all shot attempts (goals, shots on goal, missed shots, blocked shots), filtered by situation. Excludes shootout attempts.',
        1),
    ('FENWICK', 'standard_v1',
        'Standard Fenwick definition: unblocked shot attempts (goals, shots on goal, missed shots). Excludes blocked shots and shootout attempts.',
        1),
    ('GSAX', 'naive_v1',
        'Goals saved above expected, derived from the XG naive_v1 model: sum(ExpectedGoalsAgainst) - sum(GoalsAgainst) per goalie/situation.',
        1);
GO

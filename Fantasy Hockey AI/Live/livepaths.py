"""Where the live tools read and write -- the locations only Live/ uses.

Shared data locations (ModelFeatures' features, Projections' reports, Settings/) stay in
Season/'s `paths`, which Live/ imports through `seasonlayer`. This module is named `livepaths`,
not `paths`, so it can never shadow that one (see seasonlayer.py).

    reports/<league>/   that league's draft boards, draft-assistant view and daily plans (gitignored)
    fixtures/   made-up leagues for exercising the tools before a league has rosters
"""

from pathlib import Path

import seasonlayer  # noqa: F401 -- Season/ on sys.path
import paths as season_paths

LIVE_DIR = Path(__file__).resolve().parent
REPORTS_DIR = LIVE_DIR / "reports"
FIXTURES_DIR = LIVE_DIR / "fixtures"


def ensure(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def injury_risk() -> Path:
    """Published injury-risk lists per season, e.g. Dobber's Band-Aid Boys (build_players.py)."""
    return season_paths.FEATURES_DIR / "injury_risk.parquet"


def injury_status() -> Path:
    """Each player's latest merged injury report (Live.PlayerStatus, build_players.py)."""
    return season_paths.FEATURES_DIR / "injury_status.parquet"


def player_teams() -> Path:
    """The team each player is signed with, per season (Reference.PlayerTeamHistory's open
    stints, from his NHL page; build_players.py)."""
    return season_paths.FEATURES_DIR / "player_teams.parquet"


def platform_ids() -> Path:
    """A fantasy platform's own player id -> player_id, per season (ModelFeatures/build_players.py)."""
    return season_paths.FEATURES_DIR / "platform_ids.parquet"


def league_reports(league: str) -> Path:
    """One league's outputs -- nothing one league writes can overwrite another's."""
    return REPORTS_DIR / league


def draft_board(season: str, league: str, scoring: str, suffix: str) -> Path:
    return league_reports(league) / f"draft_board_{season}_{scoring}.{suffix}"


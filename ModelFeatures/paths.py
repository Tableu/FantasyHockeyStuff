"""Where the feature builds read and write. Everything lives under ModelFeatures/data/,
which the repo's .gitignore excludes -- the parquets are derived artefacts, rebuildable from
the database in minutes.

LINEUPS_DIR holds the lockout-time lineup features (build_lineup_features.py) and the churn
calibration they are generated from; FEATURES_DIR holds the skater feature table
(build_feature_table.py), which reads the lineup parquets as its candidate universe.
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
LINEUPS_DIR = DATA_DIR / "lineups"
FEATURES_DIR = DATA_DIR / "features"
DOCS_DIR = PROJECT_ROOT / "docs"


def ensure(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return directory

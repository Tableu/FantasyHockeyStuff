"""Where the feature builds read and write. Everything lives under ModelFeatures/data/,
which the repo's .gitignore excludes -- the parquets are derived artefacts, rebuildable from
the database in minutes.

LINEUPS_DIR holds the lockout-time lineup features (build_lineup_features.py) and the churn
calibration they are generated from; FEATURES_DIR holds the skater feature table
(build_feature_table.py), which reads the lineup parquets as its candidate universe.
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
LINEUPS_DIR = DATA_DIR / "lineups"
FEATURES_DIR = DATA_DIR / "features"
DOCS_DIR = PROJECT_ROOT / "docs"


def ensure(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def write_parquet(frame, path) -> None:
    """Write then swap in: the plan server reads these files while the nightly job and its own
    refreshes rebuild them, and a reader caught mid-write gets a half file ("Parquet magic bytes
    not found", the Goals tab 2026-10-03). os.replace is atomic on one filesystem."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    frame.to_parquet(tmp, index=False)
    os.replace(tmp, path)

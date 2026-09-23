"""Where the projection models read and write.

The feature tables are built by the sibling `ModelFeatures/` folder and read from here by
path -- nothing in `Projections/` opens a database connection, so a model run cannot touch
NHLStats even by accident. `--features-dir` overrides the default for anyone keeping the
parquets elsewhere.

MODELS_DIR and REPORTS_DIR hold derived artefacts (boosters, metrics, predictions) and are
gitignored; DOCS_DIR holds the committed model cards. SCORESETS_DIR is the shared
`LeagueSettings/scoring/` folder, whose files `weights.py` can load by name.
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
FEATURES_DIR = PROJECT_ROOT.parent / "ModelFeatures" / "data" / "features"
MODELS_DIR = PROJECT_ROOT / "models"
REPORTS_DIR = PROJECT_ROOT / "reports"
DOCS_DIR = PROJECT_ROOT / "docs"
SCORESETS_DIR = PROJECT_ROOT.parent / "LeagueSettings" / "scoring"


def ensure(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def feature_table(season: str, variant: str, features_dir: Path | None = None) -> Path:
    return (features_dir or FEATURES_DIR) / f"skaters_{variant}_{season}.parquet"

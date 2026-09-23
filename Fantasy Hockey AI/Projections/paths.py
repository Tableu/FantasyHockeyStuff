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


def predictions(variant: str, season: str, walk_forward: bool = False) -> Path:
    """A scored holdout's per-row predictions, keyed by the season it held out.

    Keyed so that a build holding out 2024-25 cannot overwrite the 2025-26 one, and so that a
    reader asking for one season can never be handed the other. The unkeyed name used to be the
    only name, and `Season/inputs.load_projections("2024-25")` quietly returned 2025-26.
    """
    kind = "predictions_walkforward_" if walk_forward else "predictions_"
    return REPORTS_DIR / f"{kind}{variant}_{season}.parquet"


def feature_table(season: str, variant: str, features_dir: Path | None = None) -> Path:
    return (features_dir or FEATURES_DIR) / f"skaters_{variant}_{season}.parquet"

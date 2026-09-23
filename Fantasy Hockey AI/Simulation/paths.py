"""Where the simulation layer reads and writes.

Section 5 starts from the lambda table `Projections/predict.py` writes and from the
dispersion `Projections/calibrate.py` fits. Both are read **by path** -- nothing here
imports from `Projections/`, and like that folder this one never opens a database
connection. `--lambdas`, `--dispersion` and `--scoreset` override every default.

REPORTS_DIR holds the fitted correlation structure and the simulated summaries and is
gitignored; DOCS_DIR holds committed prose.
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
PROJECTIONS_DIR = PROJECT_ROOT.parent / "Projections"
PROJECTIONS_REPORTS = PROJECTIONS_DIR / "reports"
SCORESETS_DIR = PROJECT_ROOT.parent / "LeagueSettings" / "scoring"
REPORTS_DIR = PROJECT_ROOT / "reports"
DOCS_DIR = PROJECT_ROOT / "docs"

DISPERSION_PATH = PROJECTIONS_REPORTS / "dispersion.json"
CORRELATIONS_PATH = REPORTS_DIR / "correlations.json"


def ensure(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def lambda_table(season: str, variant: str = "A") -> Path:
    return PROJECTIONS_REPORTS / f"lambdas_{season}_{variant}.parquet"


def holdout_predictions(season: str, variant: str = "B") -> Path:
    """The scored holdout the correlation structure is measured from, for the season it held out.
    Same name as `Projections/paths.predictions` -- keyed so one season's fit never reads another's."""
    return PROJECTIONS_REPORTS / f"predictions_{variant}_{season}.parquet"

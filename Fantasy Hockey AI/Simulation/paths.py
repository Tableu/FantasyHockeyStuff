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
SCORESETS_DIR = PROJECT_ROOT.parent / "Settings" / "scoring"
REPORTS_DIR = PROJECT_ROOT / "reports"
DOCS_DIR = PROJECT_ROOT / "docs"
FEATURES_DIR = PROJECT_ROOT.parent / "ModelFeatures" / "data" / "features"

# Fitted on one holdout season's residuals, so keyed by it. Replaying another season with these
# would carry that season's fit into the replay -- for a section 11 run on 2024-25, final-holdout
# information in the tuning.
def dispersion_path(season: str = "2025-26") -> Path:
    return PROJECTIONS_REPORTS / f"dispersion_{season}.json"


def correlations_path(season: str = "2025-26") -> Path:
    return REPORTS_DIR / f"correlations_{season}.json"


def goalie_fit_path(season: str = "2025-26") -> Path:
    """The goalie sampler's fitted numbers FOR `season`, fitted on the seasons before it."""
    return REPORTS_DIR / f"goalie_fit_{season}.json"


def ensure(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def lambda_table(season: str, variant: str = "A") -> Path:
    return PROJECTIONS_REPORTS / f"lambdas_{season}_{variant}.parquet"


def holdout_predictions(season: str, variant: str = "B") -> Path:
    """The scored holdout the correlation structure is measured from, for the season it held out.
    Same name as `Projections/paths.predictions` -- keyed so one season's fit never reads another's."""
    return PROJECTIONS_REPORTS / f"predictions_{variant}_{season}.parquet"

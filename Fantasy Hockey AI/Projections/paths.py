"""Where the projection models read and write.

The feature tables are built by the sibling `ModelFeatures/` folder and read from here by
path -- nothing in `Projections/` opens a database connection, so a model run cannot touch
NHLStats even by accident. `--features-dir` overrides the default for anyone keeping the
parquets elsewhere.

MODELS_DIR and REPORTS_DIR hold derived artefacts (boosters, metrics, predictions) and are
gitignored. Boosters are filed by the season they PREDICT (`models_dir`): the build that holds out
2025-26 lives in models/2025-26/, and a deployment build trained through 2025-26 -- the one shipped
for next season -- in models/2026-27/. Inside a season, one folder per model family: skaters/<A|B>/
(the per-game chain), goalie_start/, and ros_<horizon>/ (rest-of-season); DOCS_DIR holds the committed model cards. SCORESETS_DIR is the shared
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


def next_season(season: str) -> str:
    """ "2025-26" -> "2026-27". """
    first = int(season[:4]) + 1
    return f"{first}-{str(first + 1)[2:]}"


def target_season(train_seasons, holdout_season=None) -> str:
    """The season a build predicts: its holdout, or -- for a deployment build, which holds nothing
    out -- the season after the last one it trained on."""
    return holdout_season or next_season(max(train_seasons))


MODEL_FAMILIES = ("skaters", "goalie_start")    # plus ros_<horizon>, e.g. ros_season, ros_42d


def models_dir(season: str, family: str, variant: str | None = None) -> Path:
    """Where one family of boosters that predict `season` lives: models/<season>/<family>/, plus a
    variant folder (A or B) for the skater chain.

    One folder per predicted season means a scored build and a deployment build can never
    overwrite each other; one per family means a season's folder says what it holds, and a model
    that was never built for that season shows up as a missing folder rather than hiding beside
    an unrelated one."""
    if family not in MODEL_FAMILIES and not family.startswith("ros_"):
        raise ValueError(f"unknown model family {family!r}: {MODEL_FAMILIES} or ros_<horizon>")
    if (family == "skaters") != (variant is not None):
        raise ValueError("the skater chain needs a variant (A or B); no other family has one")
    directory = MODELS_DIR / season / family
    return directory / variant if variant else directory


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

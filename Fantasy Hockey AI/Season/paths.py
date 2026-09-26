"""Where the season simulator reads and writes.

Section 6 starts from files three siblings produced and opens no database of its own:

    ModelFeatures/data/features/   the candidate universe, the realized skater lines, the
                                   per-player availability flag, the goalie start lines
    ModelFeatures/data/lineups/    the goalie candidate set and who actually started
    Projections/reports/           the projections, out of sample on the simulated season
    Settings/scoring/        what a goal is worth
    Settings/rosters/        the format: slots, bench, IR, moves, playoffs

It is the first folder in the stack that also imports *code* from a sibling -- `Simulation`'s
sampler, scoring and copula -- because drawing a night is a computation, not a file. That one
dependency is declared in `engine.py` and nowhere else; everything else arrives by path.

REPORTS_DIR holds the ladder results and is gitignored; DOCS_DIR holds committed prose that
`report.py` generates rather than anyone typing.
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SIBLINGS = PROJECT_ROOT.parent

SIMULATION_DIR = SIBLINGS / "Simulation"
# The decision policies -- managers, the lineup solver, the draft rule. Code, not files, so it is
# reached through `decisionlayer.py` and nowhere else.
DECISIONS_DIR = SIBLINGS / "Decisions"
PROJECTIONS_DIR = SIBLINGS / "Projections"
PROJECTIONS_REPORTS = PROJECTIONS_DIR / "reports"
# NOTE: `Simulation/scoring.py` does `import paths` and reads SCORESETS_DIR from whichever paths
# module was imported first -- which, with this folder on sys.path, is this one. So this constant
# has to keep pointing where Simulation/paths.py points. `simlayer.assert_scoreset_dirs_agree`
# checks it rather than trusting it.
SETTINGS_DIR = SIBLINGS / "Settings"
SCORESETS_DIR = SETTINGS_DIR / "scoring"
ROSTERS_DIR = SETTINGS_DIR / "rosters"
FEATURES_DIR = SIBLINGS / "ModelFeatures" / "data" / "features"
LINEUPS_DIR = SIBLINGS / "ModelFeatures" / "data" / "lineups"

REPORTS_DIR = PROJECT_ROOT / "reports"
DOCS_DIR = PROJECT_ROOT / "docs"

LEAGUE_CONFIG = ROSTERS_DIR / "league.json"

# The strategy parameters (horizons, margins, streaming, priors): what a manager chooses, as
# against scoring/ and rosters/, what the league imposes. A --strategy name is looked up here.
STRATEGY_DIR = SETTINGS_DIR
STRATEGY_CONFIG = STRATEGY_DIR / "strategy.json"
# The simulated opponents: how the managers who are not ours draft (field.py).
FIELD_CONFIG = SETTINGS_DIR / "field.json"

# The Monte Carlo layer's two fitted files, read by `simlayer.py` so that nothing here has to
# import Simulation's own `paths` module -- which it cannot, because this one shadows it.

# Fitted on one holdout season's residuals, so keyed by it. Replaying another season with these
# would carry that season's fit into the replay -- for a section 11 run on 2024-25, final-holdout
# information in the tuning.
def dispersion_path(season: str) -> Path:
    return PROJECTIONS_REPORTS / f"dispersion_{season}.json"


def goalie_fit_path(season: str) -> Path:
    return SIMULATION_DIR / "reports" / f"goalie_fit_{season}.json"


def correlations_path(season: str) -> Path:
    return SIMULATION_DIR / "reports" / f"correlations_{season}.json"


def ensure(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def base_table(season: str) -> Path:
    """The candidate universe and the realized skater lines."""
    return FEATURES_DIR / f"base_{season}.parquet"


def skater_features(season: str, variant: str = "A") -> Path:
    """Carries `injured_at_lockout`, the persistent absence layer for skaters."""
    return FEATURES_DIR / f"skaters_{variant}_{season}.parquet"


def lineup_features(season: str, variant: str = "A") -> Path:
    """The goalie candidate set, `injured_at_lockout` for goalies, and who started."""
    return LINEUPS_DIR / f"features_{variant}_{season}.parquet"


def goalie_starts(season: str) -> Path:
    """The realized per-start goalie line (ModelFeatures/build_goalie_starts.py)."""
    return FEATURES_DIR / f"goalie_starts_{season}.parquet"


def fantasy_positions(platform: str, season: str) -> Path:
    """Platform positional eligibility (ModelFeatures/build_fantasy_positions.py)."""
    return FEATURES_DIR / f"fantasy_positions_{platform.lower()}_{season}.parquet"


def fantasy_adp(platform: str, season: str) -> Path:
    return FEATURES_DIR / f"fantasy_adp_{platform.lower()}_{season}.parquet"


def external_projections(season: str) -> Path:
    """Every external source's preseason projections (ModelFeatures/build_external_projections.py)."""
    return FEATURES_DIR / f"external_projections_{season}.parquet"


def players() -> Path:
    """Player names and NHL positions, for boards a person reads (ModelFeatures/build_players.py)."""
    return FEATURES_DIR / "players.parquet"


def teams() -> Path:
    return FEATURES_DIR / "teams.parquet"


def schedule(season: str) -> Path:
    """The season's NHL regular-season schedule (ModelFeatures/build_schedule.py)."""
    return FEATURES_DIR / f"schedule_{season}.parquet"





def holdout_predictions(season: str, variant: str = "A") -> Path:
    """The projections the backtest runs on.

    Deliberately NOT `lambdas_<season>_<variant>.parquet`. That table is whatever is in
    `Projections/predict.py` was pointed at, by default the deployment build trained on all
    three seasons -- in-sample on the season being simulated. This file is the holdout build's
    scored season, which is the only honest input for a strategy comparison. `inputs.py`
    asserts it. Keyed by the season held out: the unkeyed name made `load_projections("2024-25")`
    return 2025-26 without a word.
    """
    return PROJECTIONS_REPORTS / f"predictions_{variant}_{season}.parquet"


def ros_predictions(season: str, horizon: str = "season") -> Path:
    """Rest-of-season projections from a build that held `season` out.

    Written by `Projections/ros_train.py --predictions-out`. Not `ros_projections_*.parquet`,
    which `ros_predict.py` makes from the deployment build -- trained on the season being replayed.
    """
    return PROJECTIONS_REPORTS / f"ros_predictions_{horizon}_{season}.parquet"


def scoreset(name: str) -> Path:
    path = Path(name)
    return path if path.exists() else SCORESETS_DIR / f"{path.stem}.json"



def strategy_config(name: str) -> Path:
    path = Path(name)
    return path if path.exists() else STRATEGY_DIR / f"{path.stem}.json"


def field_config(name: str) -> Path:
    path = Path(name)
    return path if path.exists() else SETTINGS_DIR / f"{path.stem}.json"


def league_config(name: str) -> Path:
    path = Path(name)
    return path if path.exists() else ROSTERS_DIR / f"{path.stem}.json"


def ladder_report(season: str, config_name: str = None) -> Path:
    """One report per season AND per league config.

    The config belongs in the name because a format change is the point of having the config be
    data: two runs under different formats are different measurements, and a shared filename makes
    the second silently destroy the first -- which it did once already.
    """
    suffix = f"_{config_name}" if config_name else ""
    return REPORTS_DIR / f"ladder_{season}{suffix}.json"

"""The league registry: Settings/leagues/<name>.json, one file per league we play.

Every Live tool takes `--league <name>` and gets from here what used to be separate defaults:

    platform              fleaflicker | espn | standalone (no API: the draft tools need --slot)
    league_id             the platform's league id (None until an ESPN league is joined)
    season                "2026-27"
    team                  {"id", "name"} as the platform shows them, or None
    rules                 a Settings/rosters/ name (slots, bench, IR, moves, waivers, playoffs)
    scoring               a Settings/scoring/ name
    strategy              a Settings/ strategy name, or None for strategy.json
    eligibility_platform  whose positions players are valued on
    adp_platform          whose ADP the draft tools show beside the board
    active                whether scheduled jobs run this league
    draft_window          the draft window's saved settings (its settings popup's Save as default)

Command-line flags still override a field for one run. Unknown or missing keys fail at load, as
Settings/rosters files do, so a typo cannot silently fall back to another league's value.
"""

import dataclasses
import json
from pathlib import Path

import seasonlayer  # noqa: F401 -- puts Season/ on sys.path; see seasonlayer.py
import paths as season_paths

LEAGUES_DIR = season_paths.SETTINGS_DIR / "leagues"
DEFAULT_LEAGUE = "beagles"
PLATFORMS = ("fleaflicker", "espn", "standalone")
KEYS = ("name", "description", "platform", "league_id", "season", "team", "rules", "scoring",
        "strategy", "eligibility_platform", "adp_platform", "active", "draft_window")


@dataclasses.dataclass
class League:
    name: str
    description: str
    platform: str
    league_id: int | None
    season: str
    team: dict | None
    rules: str
    scoring: str
    strategy: str | None
    eligibility_platform: str
    adp_platform: str
    active: bool
    draft_window: dict
    path: Path

    @property
    def team_id(self):
        return self.team["id"] if self.team else None

    @property
    def team_name(self):
        return self.team["name"] if self.team else None

    @property
    def readable(self) -> bool:
        """Whether the draft tools can follow this league's draft from its platform (today:
        Fleaflicker only; ESPN's reader is the plan's Step 2)."""
        return self.platform == "fleaflicker" and self.league_id is not None


def path_for(name: str) -> Path:
    path = Path(name)
    return path if path.suffix == ".json" and path.exists() else LEAGUES_DIR / f"{path.stem}.json"


def load(name: str = DEFAULT_LEAGUE) -> League:
    path = path_for(name)
    if not path.exists():
        known = ", ".join(sorted(p.stem for p in LEAGUES_DIR.glob("*.json"))) or "none"
        raise SystemExit(f"no league {name!r} in {LEAGUES_DIR} (have: {known})")
    raw = json.loads(path.read_text(encoding="utf-8"))
    missing, extra = set(KEYS) - set(raw), set(raw) - set(KEYS)
    if missing or extra:
        raise ValueError(f"{path.name}: missing {sorted(missing)}, unknown {sorted(extra)}")
    if raw["platform"] not in PLATFORMS:
        raise ValueError(f"{path.name}: platform {raw['platform']!r}; use one of {PLATFORMS}")
    if raw["name"] != path.stem:
        raise ValueError(f"{path.name}: name {raw['name']!r} must match the file name")
    return League(**raw, path=path)


def all_leagues() -> list:
    return [load(p.stem) for p in sorted(LEAGUES_DIR.glob("*.json"))]


def active() -> list:
    return [league for league in all_leagues() if league.active]


def save_draft_window(league: League, settings: dict) -> Path:
    """Write the draft window's saved settings into the league's file, leaving the rest as it is."""
    raw = json.loads(league.path.read_text(encoding="utf-8"))
    raw["draft_window"] = settings
    league.path.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    league.draft_window = settings
    return league.path

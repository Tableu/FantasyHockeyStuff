"""The league registry: one file per league the server plans --

    Settings/leagues/<name>.json    the leagues set up by hand (beagles, espn-la: tuned strategies,
                                    overrides), kept in git
    state/leagues/<name>.json       the leagues users add in the app (Server/state/, gitignored;
                                    FH_STATE_DIR for a test server's own), named <fl|espn>-<league
                                    id>-<team id>: their detected rules and scoring beside them in
                                    rosters/ and scoring/, a private ESPN league's cookies in
                                    state/league_secrets.json (league_secrets.py)

A user adding a team that is set up already (in either folder) is linked to it (users.py).

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
    auth                  a key into Settings/secrets.json (gitignored) for a private league's login,
                          e.g. ESPN's espn_s2 + SWID cookies; None for a public league
    overrides             settings no platform reports, laid over the detected ones by
                          sync_league_settings.py (e.g. {"schedule": {"min_first_week_games": 20}})
    draft_window          the draft window's saved settings (its settings popup's Save as default)

Command-line flags still override a field for one run. Unknown or missing keys fail at load, as
Settings/rosters files do, so a typo cannot silently fall back to another league's value.
"""

import dataclasses
import json
import os
import shutil
import sys
from pathlib import Path

import corelayer  # noqa: F401 -- puts Core/ on sys.path; see corelayer.py
import jsonfile
import paths as season_paths

LEAGUES_DIR = season_paths.SETTINGS_DIR / "leagues"
ENGINE_DIR = Path(__file__).resolve().parent
DEFAULT_LEAGUE = "beagles"
PLATFORMS = ("fleaflicker", "espn", "standalone")
KEYS = ("name", "description", "platform", "league_id", "season", "team", "rules", "scoring",
        "strategy", "eligibility_platform", "adp_platform", "active", "auth", "overrides",
        "draft_window")


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
    auth: str | None
    overrides: dict
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
        """Whether the tools can read this league from its platform (platforms/)."""
        return self.platform in ("fleaflicker", "espn") and self.league_id is not None


def state_root() -> Path:
    """Server/state/, or FH_STATE_DIR (a test server's own)."""
    return Path(os.environ.get("FH_STATE_DIR") or ENGINE_DIR / "state")


def state_dir() -> Path:
    """Where the leagues added in the app live."""
    return state_root() / "leagues"


def league_dirs() -> list:
    return [LEAGUES_DIR, state_dir()]


def path_for(name: str) -> Path:
    path = Path(name)
    if path.suffix == ".json" and path.exists():
        return path
    for folder in league_dirs():
        if (folder / f"{path.stem}.json").exists():
            return folder / f"{path.stem}.json"
    return LEAGUES_DIR / f"{path.stem}.json"


def is_added(league) -> bool:
    """Whether the league was added in the app (its file in state/leagues/)."""
    return league.path.parent.resolve() == state_dir().resolve()


def settings_dirs(league) -> tuple:
    """(rosters folder, scoring folder) the league's detected settings are written to."""
    if is_added(league):
        return state_dir() / "rosters", state_dir() / "scoring"
    return season_paths.ROSTERS_DIR, season_paths.SCORESETS_DIR


def load(name: str = DEFAULT_LEAGUE) -> League:
    path = path_for(name)
    if not path.exists():
        known = ", ".join(sorted(p.stem for folder in league_dirs() for p in folder.glob("*.json"))) or "none"
        raise SystemExit(f"no league {name!r} in {LEAGUES_DIR} or {state_dir()} (have: {known})")
    raw = json.loads(path.read_text(encoding="utf-8"))
    missing, extra = set(KEYS) - set(raw), set(raw) - set(KEYS)
    if missing or extra:
        raise ValueError(f"{path.name}: missing {sorted(missing)}, unknown {sorted(extra)}")
    if raw["platform"] not in PLATFORMS:
        raise ValueError(f"{path.name}: platform {raw['platform']!r}; use one of {PLATFORMS}")
    if raw["name"] != path.stem:
        raise ValueError(f"{path.name}: name {raw['name']!r} must match the file name")
    league = League(**raw, path=path)
    if is_added(league):
        # Its rules and scoring beside it, as paths (the loaders take a path as well as a name),
        # made here so the file holds names and works on the PC and in the container alike.
        rosters, scoring = settings_dirs(league)
        league.rules = str(rosters / f"{raw['rules']}.json")
        league.scoring = str(scoring / f"{raw['scoring']}.json")
    return league


SECRETS = season_paths.SETTINGS_DIR / "secrets.json"


def credentials(league: League) -> dict:
    """The league's login, or {} for a public league: an added league's from
    state/league_secrets.json (encrypted; league_secrets.py), else Settings/secrets.json (never
    committed). Fails with what to do when the file or the entry is missing, or still holds the
    placeholders."""
    if league.auth is None:
        return {}
    if is_added(league):
        import league_secrets
        entry = league_secrets.get(league.auth)
        if not entry:
            raise SystemExit(f"{league.name}: its ESPN cookies are gone -- add the league again in the app")
        return entry
    if not SECRETS.exists():
        raise SystemExit(f"{league.name} needs a login ({league.auth!r}) but {SECRETS} does not exist")
    entry = json.loads(SECRETS.read_text(encoding="utf-8")).get(league.auth)
    if not entry:
        raise SystemExit(f"no {league.auth!r} entry in {SECRETS}")
    unfilled = [k for k, v in entry.items() if not v or str(v).startswith("PASTE_")]
    if unfilled:
        raise SystemExit(f"{SECRETS} {league.auth!r}: fill in {unfilled} (see the file's _note)")
    return entry


def all_leagues() -> list:
    """Settings/leagues/'s, the hand-made leagues (the pipeline's importers and the offline
    checks loop over these). `everything()` adds the leagues added in the app."""
    return [load(p.stem) for p in sorted(LEAGUES_DIR.glob("*.json"))]


def everything() -> list:
    """Every league in both folders; a file that does not load is left out (and said), so one
    broken file cannot stop the others."""
    out = []
    for folder in league_dirs():
        for path in sorted(folder.glob("*.json")):
            try:
                out.append(load(path.stem))
            except (SystemExit, ValueError, KeyError, TypeError) as error:
                print(f"league file {path.name} not loaded: {error}", file=sys.stderr)
    return out


def index() -> dict:
    """{(platform, league id, team id): League} for every league set up with a team, read once
    (the add screen matches a whole league's teams against it)."""
    return {(lg.platform, lg.league_id, lg.team_id): lg for lg in everything()
            if lg.league_id is not None and lg.team_id is not None}


def find(platform: str, league_id: int, team_id: int, known: dict | None = None):
    """The league already set up for this team (in either folder), or None. `known`: an index()
    already read."""
    return (known if known is not None else index()).get((platform, int(league_id), int(team_id)))


# What no platform reports and the harness needs, for a league added in the app: as beagles and
# espn-la set it (a first week with under 20 NHL games is merged into the second).
ADDED_OVERRIDES = {"schedule": {"min_first_week_games": 20}}


def added_name(platform: str, league_id: int, team_id: int) -> str:
    return f"{'fl' if platform == 'fleaflicker' else platform}-{int(league_id)}-{int(team_id)}"


def create(platform: str, league_id: int, team_id: int, team_name: str, season: str,
           description: str, private: bool) -> League:
    """A new league in state/leagues/ (its rules and scoring are written by sync_league_settings,
    under its name). `private`: an ESPN league whose cookies league_secrets keeps under its name."""
    name = added_name(platform, league_id, team_id)
    folder = state_dir()
    folder.mkdir(parents=True, exist_ok=True)
    raw = {"name": name, "description": description, "platform": platform, "league_id": int(league_id),
           "season": season, "team": {"id": int(team_id), "name": team_name}, "rules": name,
           "scoring": name, "strategy": None, "eligibility_platform": platform,
           "adp_platform": "espn", "active": True, "auth": name if private else None,
           "overrides": ADDED_OVERRIDES, "draft_window": {}}
    jsonfile.write(folder / f"{name}.json", raw, indent=2)
    return load(name)


def delete(league: League) -> None:
    """An added league's files: its registry file, rules, scoring, cookies and reports. The
    hand-made leagues (Settings/leagues/) are never deleted."""
    if not is_added(league):
        return
    import league_secrets
    import livepaths
    rosters, scoring = settings_dirs(league)
    for path in (league.path, rosters / f"{league.name}.json", scoring / f"{league.name}.json"):
        path.unlink(missing_ok=True)
    if league.auth:
        league_secrets.remove(league.auth)
    shutil.rmtree(livepaths.league_reports(league.name), ignore_errors=True)


def active() -> list:
    return [league for league in all_leagues() if league.active]


def save_draft_window(league: League, settings: dict) -> Path:
    """Write the draft window's saved settings into the league's file, leaving the rest as it is."""
    raw = json.loads(league.path.read_text(encoding="utf-8"))
    raw["draft_window"] = settings
    league.path.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    league.draft_window = settings
    return league.path

"""One plan pass, as steps run_live.py (the terminal) and the plan server (Server/server.py,
which plan_gui.py shows) call. Nothing here is scheduled -- a pass runs when one of them asks.

    snapshots(kinds)   fresh injury / line-chart / starting-goalie reports into the Live schema
                       (pipeline/snapshot_live.py -- lines and goalies run only from here; injuries
                       are also scheduled, 10:00 and 15:00), then, after injuries or lines, the
                       merged status re-exported for the draft window's Status column
                       (ModelFeatures/build_players.py)
    tonight(day)       tonight's rows and projections (ModelFeatures/build_tonight.py, then
                       Projections/project_tonight.py) and every goalie's projected start share
                       (Projections/goalie_workload.py); on a day with no games, no projections
    read_league(...)   the league as its platform shows it now (or a snapshot file)
    plan(...)          the shipped manager's plan (live.LiveRunner)
    save(...)          reports/<league>/plans/plan_{date}_{stem}.md + .json + plan_latest.md

Planner runs them as one refresh (full, quick, news, league or plan: Planner.run) for every
league the server plans, or one -- the data steps once while each league's settings check and
read run alongside (a read-only child: the read needs none of the data steps), then each
league's plan and save on that read. Each step
reports progress through `echo` (print by default; the server passes its job's progress lines).

The leagues are planned in a child process, as the data steps already are:

    python planpass.py --league beagles --league espn-la --date 2026-10-02 [--now ...]

reads, plans and saves each league in turn (plan_leagues), printing its progress and, last, each
league's result, then exits. A plan's working memory -- building a board and a sampler, planning --
is a few hundred MB that a long-running process would keep after use (2026-10-02: the server held
about 480 MiB with two leagues planned, of which under 1 MB per league was anything it kept); a
child returns it at exit, so the server's memory no longer grows with the leagues it plans. Building
a league's board and sampler takes about half a second, so nothing is kept between refreshes.
"""

import argparse
import collections
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

import seasonlayer  # noqa: F401 -- puts Season/ on sys.path; see seasonlayer.py
import live
import livepaths
import paths
import platforms
import sync_league_settings

WINDOW_LEAD = dt.timedelta(minutes=30)
MODEL_FEATURES = paths.SIBLINGS / "ModelFeatures"
PIPELINE = paths.SIBLINGS.parent / "pipeline"
SNAPSHOT_KINDS = ("injuries", "lines", "goalies")
# A quick refresh (the Quick button, an auto window with fresh line charts) skips only the line charts. Injuries ride
# along since the Fleaflicker listing reads its pages in parallel (~5 s instead of ~16 s), so a
# late scratch posted as an injury reaches the next window's plan.
QUICK_SNAPSHOT_KINDS = ("injuries", "goalies")
MODES = ("full", "quick", "news", "league", "plan")   # Planner.run's refreshes
PLAN_TIMEOUT_S = 600            # a planning child still running after this is stopped
CHILD_ECHO, CHILD_RESULT = "@@echo ", "@@result "     # the planning child's progress and result lines


def _run(command, cwd, echo, label):
    """A sibling folder's script, its last line echoed; a failure raises with its stderr tail."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    done = subprocess.run([sys.executable, *command], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)
    lines = [l for l in (done.stdout + done.stderr).splitlines() if l.strip()]
    if done.returncode != 0:
        raise RuntimeError(f"{label} failed:\n" + "\n".join(lines[-8:]))
    useful = [l for l in lines if " INFO " in l or " WARNING " in l]
    echo(f"{label}: {(useful or lines or ['done'])[-1].split(' INFO ')[-1].split(' WARNING ')[-1]}")
    return lines


def snapshots(kinds=SNAPSHOT_KINDS, echo=print, force_goalies=False, injuries_max_age=None,
              injuries_targeted=False) -> set:
    """Returns the kinds fetched. `injuries_max_age` (minutes): reuse an injury report that recent
    instead of fetching it. `injuries_targeted`: Fleaflicker's rostered players and its likely
    injured by id (about 4 calls), never its whole 44-page listing, which the 10:00 and 15:00 runs
    take at least every 20 h. The goalie report runs alongside the others (~1 s): injuries and
    line charts each recompute the merged status table, so those two stay one after the other,
    while the goalie report writes only its own tables."""
    def one(kind):
        """The kind if fetched, None if reused or (line charts only) failed."""
        command = ["snapshot_live.py", "--kind", kind] + (["--force"] if kind == "goalies" and force_goalies else [])
        if kind == "injuries" and injuries_max_age is not None:
            command += ["--max-age", str(injuries_max_age)]
        if kind == "injuries" and injuries_targeted:
            command += ["--targeted"]
        try:
            lines = _run(command, PIPELINE, echo, f"snapshot {kind}")
        except RuntimeError as error:
            if kind != "lines":
                raise
            # A plan on the last line charts beats none (an auto window runs Full when they're
            # stale); not recorded as fetched, so the next window tries again.
            echo(f"{error}\nsnapshot lines: planning on the last line charts")
            return None
        reused = [l for l in lines if ": skipped, last snapshot" in l]
        if reused:
            echo(f"snapshot {kind}: reused -- " + "; ".join(l.split(" INFO ")[-1] for l in reused))
            return None
        return kind

    with ThreadPoolExecutor(max_workers=1) as pool:
        goalies = pool.submit(one, "goalies") if "goalies" in kinds else None
        fetched = {one(kind) for kind in kinds if kind != "goalies"}
        if {"injuries", "lines"} & set(kinds):
            _run(["build_players.py"], MODEL_FEATURES, echo, "injury status export")
        if goalies is not None:
            fetched.add(goalies.result())      # its failure raises here, as before
    return fetched - {None}


def tonight(day: dt.date, echo=print) -> bool:
    """Tonight's rows and projections; False on a day with no games, which still gets its injury
    report and a plan (roster, IR, pickups -- no lineup)."""
    _run(["build_tonight.py", "--date", day.isoformat()], MODEL_FEATURES, echo, "tonight's rows")
    # Every goalie's projected share of his team's remaining starts, as of today (the rest-of-
    # season goalie value; also on a day with no games, for the upgrade and drop decisions).
    _run(["goalie_workload.py", "--build", "--season", live.season_of(day), "--date", day.isoformat()],
         paths.PROJECTIONS_DIR, echo, "goalie workload")
    if not (live.STATUS_DIR / f"tonight_{day.isoformat()}_skaters.parquet").exists():
        echo(f"no regular-season NHL games on {day.isoformat()}: planning moves, no lineup")
        return False
    _run(["project_tonight.py", "--date", day.isoformat()], paths.PROJECTIONS_DIR, echo, "projections")
    return True


def puck_times(day: dt.date) -> list:
    """Tonight's distinct puck times (UTC), from the schedule the tonight build read."""
    context = live.STATUS_DIR / f"tonight_{day.isoformat()}_context.parquet"
    if not context.exists():
        return []
    return sorted(set(pd.to_datetime(pd.read_parquet(context)["start_time_utc"]).dropna()))


def next_window(day: dt.date, now: dt.datetime):
    """The next puck time today if it is within WINDOW_LEAD of `now`, else None."""
    upcoming = [t for t in puck_times(day) if t > pd.Timestamp(now)]
    if not upcoming or upcoming[0] - pd.Timestamp(now) > WINDOW_LEAD:
        return None
    return upcoming[0]


def read_league(league, day: dt.date, league_file=None, platform_season=None, echo=print,
                now: dt.datetime | None = None):
    if league_file:
        snapshot = live.LeagueSnapshot.load(league_file)
    else:
        adapter = platforms.for_league(league, season=platform_season)
        if adapter is None:
            raise SystemExit(f"{league.name} has no readable platform: pass --league-file")
        snapshot = live.LeagueSnapshot.from_platform(adapter, league.team_id, day, now=now)
    echo(f"league: {snapshot.source}, {len(snapshot.teams)} teams, "
         f"{len(snapshot.teams[snapshot.me]['roster'])} on my roster")
    return snapshot


def plan(runner, snapshot, now: dt.datetime, echo=print) -> dict:
    result = runner.plan(snapshot, now)
    echo(f"plan: {len(result['moves'])} move(s), {sum(1 for s in result['lineup'] if s['player'])} "
         f"in tonight's lineup, P(win) {result['p_win']:.0%}")
    return result


def save(league, result: dict, day: dt.date, stem: str, echo=print):
    plans_dir = livepaths.ensure(livepaths.league_reports(league.name) / "plans")
    path = plans_dir / f"plan_{day.isoformat()}_{stem}.md"
    path.write_text(live.render(result), encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    shutil.copyfile(path, plans_dir / "plan_latest.md")
    echo(f"saved {path.name}")
    return path


def saved_plans(league, day: dt.date) -> list:
    """The day's saved plan JSONs, oldest first."""
    plans = livepaths.league_reports(league.name) / "plans"
    return sorted(plans.glob(f"plan_{day.isoformat()}_*.json"), key=lambda f: f.stat().st_mtime)


def last_read_path(league) -> Path:
    """Where a league's last read is kept (plan_leagues), for a re-plan without one."""
    return livepaths.league_reports(league.name) / "last_read.json"


def plan_leagues(league_names, day: dt.date, now: dt.datetime, league_file=None, platform_season=None,
                 echo=print, reuse_read=False, read_only=False) -> dict:
    """Each league's read, plan and save, in this process: {league name: {"file": the saved plan's
    name, "read_at": when its league was read} or {"error": why it stopped}} -- one league's failure
    (its platform down) leaves the others' plans. Each read is kept (`last_read_path`).
    `reuse_read` plans on today's kept read instead of reading again -- True: every league's (a
    re-plan on the user's choices: about 2 s, not 9), reading only when there is none from today;
    a collection of names: those leagues' only, just read by a `read_only` pass (Planner.run).
    `read_only`: the read and its kept copy alone, no plan ({"read_at"} per league)."""
    import leagues as registry

    results = {}
    for name in league_names:
        say = echo if len(league_names) == 1 else (lambda text, name=name: echo(f"{name}: {text}"))
        try:
            league = registry.load(name)
            kept = last_read_path(league)
            reuse = reuse_read is True or (bool(reuse_read) and name in reuse_read)
            if (reuse and not read_only and not league_file and kept.exists()
                    and dt.datetime.fromtimestamp(kept.stat().st_mtime).date() == dt.date.today()):
                snapshot = live.LeagueSnapshot.load(kept)
                read_at = dt.datetime.fromtimestamp(kept.stat().st_mtime)
                say(f"league: the read from {read_at:%H:%M:%S}")
            else:
                snapshot = read_league(league, day, league_file, platform_season, say, now=now)
                read_at = dt.datetime.now()
                if not league_file:
                    snapshot.dump(livepaths.ensure(kept.parent) / kept.name)
            if read_only:
                results[name] = {"read_at": read_at.isoformat(timespec="seconds")}
                continue
            runner = live.LiveRunner(day, league)
            result = plan(runner, snapshot, now, say)
            path = save(league, result, day, f"{now:%H%M}", say)
            results[name] = {"file": path.with_suffix(".json").name, "read_at": read_at.isoformat(timespec="seconds")}
        except (Exception, SystemExit) as error:
            say(f"failed: {type(error).__name__}: {error}")
            results[name] = {"error": f"{type(error).__name__}: {error}"}
    return results


def plan_in_child(league_names, day: dt.date, now: dt.datetime, league_file=None, platform_season=None,
                  echo=print, reuse_read=False, read_only=False) -> dict:
    """plan_leagues in a child process (this file's command line), its progress echoed as it
    comes; the same result. A child that dies or overruns PLAN_TIMEOUT_S fails every league with
    the end of what it printed."""
    command = [sys.executable, str(Path(__file__).resolve()), "--date", day.isoformat(),
               "--now", now.isoformat(), *(f"--league={name}" for name in league_names)]
    if league_file:
        command.append(f"--league-file={league_file}")
    if platform_season:
        command.append(f"--platform-season={platform_season}")
    if reuse_read is True:
        command.append("--reuse-read")
    elif reuse_read:
        command += [f"--reuse-read-of={name}" for name in reuse_read]
    if read_only:
        command.append("--read-only")
    child = subprocess.Popen(command, cwd=Path(__file__).resolve().parent, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                             env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})
    overran = threading.Event()
    timer = threading.Timer(PLAN_TIMEOUT_S, lambda: (overran.set(), child.kill()))
    timer.start()
    tail, result = collections.deque(maxlen=8), None
    try:
        for line in child.stdout:
            line = line.rstrip("\n")
            if line.startswith(CHILD_ECHO):
                echo(line[len(CHILD_ECHO):])
            elif line.startswith(CHILD_RESULT):
                result = json.loads(line[len(CHILD_RESULT):])
            elif line.strip():
                tail.append(line)
        code = child.wait()
    finally:
        timer.cancel()
    if result is None:
        why = f"stopped after {PLAN_TIMEOUT_S} s" if overran.is_set() else f"exited {code}"
        error = f"planning process {why}: " + " | ".join(tail)
        echo(error)
        return {name: {"error": error} for name in league_names}
    return result


class Planner:
    """The day's refreshes as the server runs them, for one league or several: the data steps once
    (snapshots, tonight's rows and projections -- they are nobody's league), then each league's
    read, plan and save in a child process (plan_in_child). `last` is when each data step last ran;
    `league_last[name]` when that league was last read."""

    def __init__(self, leagues, day: dt.date, league_file=None, platform_season=None,
                 skip_snapshots=False):
        self.leagues, self.day = list(leagues), day
        if league_file and len(self.leagues) > 1:
            raise SystemExit("a league file is one league's snapshot: plan that league alone")
        self.league_file, self.platform_season = league_file, platform_season
        self.skip_snapshots = skip_snapshots
        self.last = {}                     # data step -> when it last ran
        self.league_last = {league.name: {} for league in self.leagues}

    def run(self, mode: str, now: dt.datetime, echo=print, only=None) -> dict:
        """One refresh of the leagues `only` (default all): {league name: its saved plan's file
        name -- or, for a league that failed (its platform down; the others still plan), the error
        as an exception}. The modes (MODES):

            full    injury, line-chart and goalie reports and tonight's projections, while each
                    league's settings check and read run alongside; then each league planned
            quick   the same without the line charts (the auto windows' usual)
            news    injury and goalie reports and projections; each league planned on today's
                    kept read (a late scratch, a goalie confirmed: nothing on the rosters moved)
            league  each league read again and planned on today's projections, no reports (a
                    move made on the platform); on a day with none yet, the projections first
            plan    each league planned again on its kept read (the user's choices changed)
        """
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
        chosen = [league for league in self.leagues if only is None or league.name in only]
        names = [league.name for league in chosen]
        if mode == "plan":
            return self._results(chosen, plan_in_child(names, self.day, now, self.league_file,
                                                       self.platform_season, echo, reuse_read=True))
        if mode == "league":
            if "projections" not in self.last:      # a Planner is one day's: none yet today
                tonight(self.day, echo)
                self.last["projections"] = dt.datetime.now()
            return self._results(chosen, plan_in_child(names, self.day, now, self.league_file,
                                                       self.platform_season, echo))
        # The leagues' settings check and (full, quick) read run while the data steps do (they
        # need none of them; a Fleaflicker read's ~4 s and the child's start hidden), then each
        # league is planned on that read. A rehearsal's league file is no read to hide.
        fresh_read = mode != "news"
        reading = None if self.league_file else self._read_in_background(chosen, now, echo, fresh_read)
        try:
            self._data_steps(mode, echo)
        finally:
            read = reading() if reading else {}
        # A league whose read failed is read again by the planning child: never planned on an
        # older read when a fresh one was asked for.
        reuse = {name for name, got in read.items() if "read_at" in got} if fresh_read else True
        return self._results(chosen, plan_in_child(names, self.day, now, self.league_file,
                                                   self.platform_season, echo, reuse_read=reuse))

    def _results(self, chosen, outcome) -> dict:
        """plan_in_child's outcome as run's result; each planned league's read time kept."""
        results = {}
        for league in chosen:
            got = outcome.get(league.name) or {"error": "the planning process said nothing about it"}
            if "error" in got:
                results[league.name] = RuntimeError(got["error"])
                continue
            self.league_last[league.name]["league read"] = dt.datetime.fromisoformat(got["read_at"])
            results[league.name] = got["file"]
        return results

    def _read_in_background(self, chosen, now, echo, read=True):
        """On its own thread: each league's settings, re-detected once a day
        (sync_league_settings.daily; the children read the files fresh), then (`read`) the
        leagues' read-only child. Returns a wait for the child's result ({} without a read)."""
        box = {}

        def work():
            for league in chosen:
                if league.readable and self.league_last[league.name].get("settings", dt.datetime.min).date() != self.day:
                    try:
                        sync_league_settings.daily(league, echo)
                        self.league_last[league.name]["settings"] = dt.datetime.now()
                    except Exception as error:  # noqa: BLE001 -- yesterday's settings still plan
                        echo(f"{league.name} settings check failed ({type(error).__name__}: {error}); "
                             "planning on the settings files as they are")
            if read:
                box["read"] = plan_in_child([league.name for league in chosen], self.day, now, None,
                                            self.platform_season, echo, read_only=True)

        thread = threading.Thread(target=work, daemon=True)
        thread.start()

        def wait():
            thread.join()
            return box.get("read", {})
        return wait

    def _data_steps(self, mode, echo):
        if not self.skip_snapshots:
            kinds = SNAPSHOT_KINDS if mode == "full" else QUICK_SNAPSHOT_KINDS
            # Every refresh fetches injuries; between full listings that is about 4 Fleaflicker
            # calls, far from the ~100-call lockout. The first refresh never takes the whole
            # 44-page listing (the 10:00 / 15:00 runs do, at least every 20 h).
            fetched = snapshots(kinds, echo, injuries_targeted=not self.last)
            for kind in fetched:                   # a reused report keeps its own time
                self.last[kind] = dt.datetime.now()
        tonight(self.day, echo)
        self.last["projections"] = dt.datetime.now()


def main():
    """The planning child (plan_in_child): progress and result as tagged lines on stdout."""
    parser = argparse.ArgumentParser(description="Plan leagues: each one's read, plan and save")
    parser.add_argument("--league", action="append", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--now", required=True, help="UTC moment for the per-game lock")
    parser.add_argument("--league-file", default=None)
    parser.add_argument("--platform-season", type=int, default=None)
    parser.add_argument("--reuse-read", action="store_true", help="plan on today's kept league read")
    parser.add_argument("--reuse-read-of", action="append", default=[], metavar="LEAGUE",
                        help="plan this league on today's kept read (repeat for several)")
    parser.add_argument("--read-only", action="store_true", help="read and keep each league, no plan")
    args = parser.parse_args()
    result = plan_leagues(args.league, dt.date.fromisoformat(args.date), dt.datetime.fromisoformat(args.now),
                          args.league_file, args.platform_season,
                          echo=lambda text: print(CHILD_ECHO + str(text), flush=True),
                          reuse_read=args.reuse_read or set(args.reuse_read_of), read_only=args.read_only)
    print(CHILD_RESULT + json.dumps(result), flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python
"""The daily plan, in a window -- the in-season counterpart of draft_gui.py, one league per run.

    python plan_gui.py                              # league beagles, today
    python plan_gui.py --league <name>
    python plan_gui.py --date 2026-09-29 --league-file fixtures/beagles/fake_league.json   # rehearsal

Nothing is scheduled: the plan runs when the window opens and while it stays open (planpass.py,
the same steps run_live.py takes):

    Full refresh    fresh injury, line-chart and goalie reports -> tonight's projections -> read the
                    league from its platform -> the shipped manager's plan. Runs on opening, where
                    an injury report under 30 minutes old is reused rather than fetched again.
    Quick refresh   goalie reports -> tonight's projections -> the plan again (lineup news, late
                    scratches) -- what the auto window runs.
    Auto window     while the window is open, a quick refresh about 30 minutes before each group of
                    games starts (Fleaflicker locks each player at his own game), once per group.

Every run is also saved as reports/<league>/plans/plan_{date}_{time}.md + .json, and on opening
today's last saved plan is shown at once while the first refresh runs. Recommend-only:
make the moves on the platform yourself.

    Tonight      tonight's lineup from the roster you hold now: expected points, chance he plays /
                 starts, puck time (local), injury / GTD flag, a lock once his game has started, and
                 who the plan would put in that slot after its moves; then his projected stat line
                 for tonight (per game, in the league's scored stats)
    Moves        IR moves, adds and drops, claims -- with the rate each was priced on
    Upgrade      permanent pickups: the plan's upgrades and claims, highlighted, above the add/drop
                 rule's own pricing of the top free agents on the roster you hold now, each with his
                 best drop, the lineup points he gains over the pricing window, the bar a move must
                 clear and the edge (gain - bar) they are ranked by (rentals: the Week tab)
    Week         the week's streaming plan (strategy mode 'week', Decisions/weekplan.py): every rental
                 it would make this week, by day, with its gain, bar and edge -- today's are the
                 Moves; the later ones are planned again on every run
    Roster       every player you hold now: status, rate, games left this week and his stats
                 (sortable); a player the plan acts on is highlighted
    Free agents  the best available now by rate, with rest-of-season points (rate x his team's games
                 left in the fantasy season; sortable), the recommended adds highlighted

Roster, Free agents and the Matchup's opponent show each player's stats in the league's scored
categories: the season so far once it has started (ModelFeatures/build_season_stats.py and the goalie
starts, both nightly), the projected season before that -- the tab title says which.

The tables show the league as it stands; the moves are only recommendations until you make them.
    Matchup      the week so far, P(win), the opponent's roster
"""

import argparse
import datetime as dt
import json
import logging
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk

import pandas as pd

import seasonlayer  # noqa: F401 -- puts Season/ on sys.path; see seasonlayer.py
import draft_board
import leagues
import live
import livepaths
import planpass
import sheets
import simlayer

AUTO_CHECK_MS = 60_000          # how often the auto window looks at the clock
OPENING_INJURIES_MAX_AGE = 30   # minutes: on opening, an injury report this fresh is reused
PLAN_COLOUR = "#e0ecff"         # a row the plan recommends acting on
STATUS_COLOURS = {"OUT": "#fde2e2", "SUSP": "#fde2e2", "DTD": "#fff4d6", "GTD": "#fff4d6"}
# A row's look by its tags: a recommended action, an injury status, greyed (a placeholder message).
ROW_STYLES = {"plan": {"bg": PLAN_COLOUR}, "empty": {"fg": "#9ca3af"},
              **{status: {"bg": colour} for status, colour in STATUS_COLOURS.items()}}


# Columns the Roster and Free agents tabs leave out of the shared player columns.
ROSTER_HIDDEN = {"per_game", "plays_tonight", "plan", "where", "ros_points"}
FREE_AGENTS_HIDDEN = {"plan", "where"}


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)


def local_time(utc_text) -> str:
    """'2026-09-29 23:00:00' (UTC) -> '4:00 PM' on this PC's clock."""
    if not utc_text or utc_text in ("None", "NaT"):
        return ""
    stamp = pd.Timestamp(utc_text).tz_localize("UTC").tz_convert(dt.datetime.now().astimezone().tzinfo)
    return stamp.strftime("%I:%M %p").lstrip("0")


def _action(plan):
    """A row's recommended action: 'drop', 'move to IR', ... or an add's kind (upgrade, rental)."""
    if not plan:
        return ""
    return plan[0].upper() + plan[1:] if plan in ("drop", "move to IR", "activate", "claim") else f"Add ({plan})"


def _stat(x):
    """A stat cell: per-game projections to two decimals, season counts as whole numbers."""
    return "" if x is None else f"{x:.2f}" if isinstance(x, float) else str(x)


def _num(x, digits=2):
    return "" if x is None or (isinstance(x, float) and pd.isna(x)) else f"{x:.{digits}f}"


class PlanWindow:
    def __init__(self, root, args):
        self.root, self.args = root, args
        self.league = leagues.load(args.league)
        self.day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
        self.runner = None                 # built once per day on the first run (the board, the sampler)
        self.plan = None
        self.busy = False
        self.windows_done = set()          # puck times already re-planned by the auto window
        self.last = {}                     # step -> when this window last ran it
        self.messages = queue.Queue()
        # Sortable tables: name -> [column key, descending]. The roster keeps the plan's order until
        # a header is clicked; the free agents start best rate first.
        self.sorts = {"roster": [None, False], "free_agents": ["rate", True]}

        root.title(f"Plan -- {self.league.name}: {self.league.team_name or 'my team'}")
        root.geometry("1400x820")
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Big.TLabel", font=("Segoe UI", 12, "bold"))
        style.configure("Warn.TLabel", foreground="#b91c1c", font=("Segoe UI", 10, "bold"))

        self._build_toolbar()
        body = ttk.PanedWindow(root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.tabs = ttk.Notebook(body)
        body.add(self.tabs, weight=4)
        scoring = simlayer.load_scoreset(self.league.scoring)
        scored = set(scoring.skaters) | set(scoring.goalies)
        self.stat_keys = [k for k in draft_board.STAT_HEADINGS if k == "gp" or k in scored]
        stat_columns = [(k, draft_board.STAT_HEADINGS[k], 48) for k in self.stat_keys]
        self.tonight = self._table("Tonight", [("slot", "Slot", 60), ("player", "Player", 240),
                                               ("mean", "Exp. pts", 80), ("sd", "SD", 60),
                                               ("p", "P(plays/starts)", 110), ("puck", "Puck", 90),
                                               ("flag", "Flag", 70), ("lock", "", 40),
                                               ("after", "After moves", 240)] + stat_columns)
        self.moves = self._table("Moves", [("kind", "Move", 110), ("add", "Add", 260), ("add_rate", "pts/g", 70),
                                           ("drop", "Drop", 260), ("drop_rate", "pts/g", 70), ("note", "Note", 200)])
        self.options = self._table("Upgrade", [("rank", "#", 36), ("kind", "Move", 90), ("add", "Add", 230),
                                               ("add_rate", "pts/g", 60), ("add_games", "Games", 60),
                                               ("drop", "Drop", 230), ("drop_rate", "pts/g", 60),
                                               ("drop_games", "Games", 60), ("gain", "Gain", 70),
                                               ("bar", "Bar", 60), ("edge", "Edge", 60), ("note", "", 150)])
        self.week = self._table("Week", [("day", "Day", 110), ("kind", "Move", 90), ("add", "Add", 230),
                                         ("add_rate", "pts/g", 60), ("add_games", "Games", 60),
                                         ("drop", "Drop", 230), ("drop_rate", "pts/g", 60),
                                         ("drop_games", "Games", 60), ("gain", "Gain", 70),
                                         ("bar", "Bar", 60), ("edge", "Edge", 60), ("note", "", 90)])
        player_columns = [("player", "Player", 240), ("positions", "Pos", 80), ("status", "Status", 70),
                          ("rate", "Rate (pts/g)", 90), ("ros_points", "ROS pts", 70),
                          ("per_game", "Tonight's proj.", 100),
                          ("plays_tonight", "Plays tonight", 95), ("games_left", "Games left", 80),
                          ("where", "", 90), ("plan", "Recommended", 110)]
        # The roster you hold: no tonight columns, no lineup/bench/IR column, no recommended action.
        self.roster = self._table("Roster", [c for c in player_columns + stat_columns
                                             if c[0] not in ROSTER_HIDDEN], sort_as="roster")
        self.free_agents = self._table("Free agents", [c for c in player_columns + stat_columns
                                                       if c[0] not in FREE_AGENTS_HIDDEN],
                                       sort_as="free_agents")
        self._build_matchup()
        body.add(self._build_sidebar(body), weight=1)

        root.after(200, self._drain)
        root.after(AUTO_CHECK_MS, self._auto_check)
        saved = self._show_saved()
        self.run("full")
        if saved:
            self.status_var.set(f"Showing the plan saved at {saved} -- full refresh running...")

    # ---------- layout ----------

    def _build_toolbar(self):
        bar = ttk.Frame(self.root, padding=(8, 8, 8, 4))
        bar.pack(fill="x")
        self.headline = tk.StringVar(value="Starting...")
        ttk.Label(bar, textvariable=self.headline, style="Big.TLabel").pack(side="left")
        self.auto = tk.BooleanVar(value=not self.args.no_auto)
        ttk.Checkbutton(bar, text="Auto: re-plan before each game window", variable=self.auto).pack(side="right", padx=8)
        self.quick_button = ttk.Button(bar, text="Quick refresh", command=lambda: self.run("quick"))
        self.quick_button.pack(side="right", padx=4)
        self.full_button = ttk.Button(bar, text="Full refresh", command=lambda: self.run("full"))
        self.full_button.pack(side="right", padx=4)
        self.status_var = tk.StringVar()
        ttk.Label(bar, textvariable=self.status_var).pack(side="right", padx=12)

    def _table(self, title, columns, sort_as=None):
        frame = ttk.Frame(self.tabs)
        self.tabs.add(frame, text=title)
        on_sort = None if sort_as is None else (lambda key: self._sort(sort_as, key))
        return sheets.Table(frame, columns, ROW_STYLES, on_sort=on_sort)

    def _build_matchup(self):
        frame = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(frame, text="Matchup")
        self.matchup_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.matchup_var, style="Big.TLabel", justify="left").pack(anchor="w")
        ttk.Label(frame, text="Opponent's roster", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(12, 2))
        columns = [("player", "Player", 260), ("positions", "Pos", 80), ("rate", "Rate (pts/g)", 100),
                   ("games_left", "Games left", 90)] + [(k, draft_board.STAT_HEADINGS[k], 48)
                                                         for k in self.stat_keys]
        table = ttk.Frame(frame)
        table.pack(fill="both", expand=True)
        self.opponent = sheets.Table(table, columns, ROW_STYLES)

    def _build_sidebar(self, parent):
        side = ttk.Frame(parent, padding=(8, 0, 0, 0))

        def listing(title, height):
            ttk.Label(side, text=title, font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(8, 2))
            box = tk.Listbox(side, height=height, activestyle="none", font=("Segoe UI", 9))
            box.pack(fill="x")
            return box
        self.goalie_box = listing("My goalies tonight", 3)
        self.watch_box = listing("Watch list", 5)
        self.problem_var = tk.StringVar()
        ttk.Label(side, textvariable=self.problem_var, style="Warn.TLabel", wraplength=320).pack(anchor="w", pady=(6, 0))
        self.fresh_var = tk.StringVar()
        ttk.Label(side, text="Data", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(8, 2))
        ttk.Label(side, textvariable=self.fresh_var, justify="left", wraplength=320).pack(anchor="w")
        ttk.Label(side, text="Progress", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(8, 2))
        self.log = tk.Text(side, height=14, width=46, font=("Consolas", 8), wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True)
        return side

    def _show_saved(self):
        """Today's last saved plan (reports/<league>/plans), shown at once while the opening
        refresh runs -- instead of an empty window for most of a minute. Returns its time, or
        None when there is none (or it predates a change to what the tables read)."""
        plans = livepaths.league_reports(self.league.name) / "plans"
        files = sorted(plans.glob(f"plan_{self.day.isoformat()}_*.json"), key=lambda f: f.stat().st_mtime)
        if not files:
            return None
        try:
            self.plan = json.loads(files[-1].read_text(encoding="utf-8"))
            self.show()
        except Exception as error:                     # noqa: BLE001 -- the refresh replaces it anyway
            self.plan = None
            self.echo(f"saved plan {files[-1].name} not shown: {type(error).__name__}: {error}")
            return None
        return dt.datetime.fromtimestamp(files[-1].stat().st_mtime).strftime("%I:%M %p").lstrip("0")

    # ---------- running a pass ----------

    def echo(self, text):
        self.messages.put(("log", f"{dt.datetime.now():%H:%M:%S}  {text}"))

    def run(self, mode):
        """Start a pass on a worker thread: 'full' or 'quick'."""
        if self.busy:
            return
        self.busy = True
        self.full_button.state(["disabled"])
        self.quick_button.state(["disabled"])
        self.status_var.set("Full refresh running..." if mode == "full" else "Quick refresh running...")
        threading.Thread(target=self._work, args=(mode,), daemon=True).start()

    def _work(self, mode):
        args = self.args
        try:
            now = dt.datetime.fromisoformat(args.now) if args.now else utc_now()
            if not args.skip_snapshots:
                kinds = planpass.SNAPSHOT_KINDS if mode == "full" else ("goalies",)
                # Opening reuses a fresh injury report (the 10:00 / 15:00 runs, a window just
                # closed); the Full refresh button always fetches.
                opening = not self.last
                fetched = planpass.snapshots(kinds, self.echo,
                                             injuries_max_age=OPENING_INJURIES_MAX_AGE if opening else None)
                for kind in fetched:                   # a reused report keeps its own time
                    self.last[kind] = dt.datetime.now()
            planpass.tonight(self.day, self.echo)
            self.last["projections"] = dt.datetime.now()
            if self.runner is None:
                self.echo("building the board and the sampler (once a day)...")
                self.runner = live.LiveRunner(self.day, self.league)
            snapshot = planpass.read_league(self.league, self.day, args.league_file, args.platform_season,
                                            self.echo, now=now)
            self.last["league read"] = dt.datetime.now()
            plan = planpass.plan(self.runner, snapshot, now, self.echo)
            planpass.save(self.league, plan, self.day, f"{now:%H%M}", self.echo)
            self.messages.put(("plan", plan))
        except BaseException as error:           # SystemExit from a step included: show it, keep the window
            self.messages.put(("error", f"{type(error).__name__}: {error}"))

    def _drain(self):
        while not self.messages.empty():
            kind, payload = self.messages.get()
            if kind == "log":
                self.log.configure(state="normal")
                self.log.insert("end", payload + "\n")
                self.log.see("end")
                self.log.configure(state="disabled")
            elif kind == "plan":
                self.plan = payload
                self._finished(f"Plan updated {dt.datetime.now():%I:%M %p}".replace(" 0", " "))
                self.show()
            elif kind == "error":
                self.echo(payload)
                self._finished("Last run failed -- see Progress")
        self.root.after(200, self._drain)

    def _finished(self, status):
        self.busy = False
        self.full_button.state(["!disabled"])
        self.quick_button.state(["!disabled"])
        self.status_var.set(status)
        self._show_freshness()

    def _auto_check(self):
        """While open: a quick refresh ~30 minutes before each group of games, once per group."""
        try:
            now = dt.datetime.fromisoformat(self.args.now) if self.args.now else utc_now()
            window = planpass.next_window(self.day, now) if self.auto.get() and not self.busy else None
            if window is not None and window not in self.windows_done:
                self.windows_done.add(window)
                self.echo(f"auto: games at {local_time(str(window))} start soon -- re-planning")
                self.run("quick")
        finally:
            self.root.after(AUTO_CHECK_MS, self._auto_check)

    # ---------- showing the plan ----------

    def show(self):
        p = self.plan
        opponent = p["opponent"] or "no opponent"
        self.headline.set(f"{p['team']} -- {p['game_date']} · week {p['week']} vs {opponent} · "
                          f"P(win) {p['p_win']:.0%} · moves left after the plan {p['moves_left']}"
                          + (" · today's moves are free" if p.get("free_moves") else "")
                          + (f" · {len(p['week_plan'])} rental(s) planned this week"
                             if p.get("stream_mode") == "week" else "")
                          + ("" if p.get("games_today", True) else " · no NHL games today"))

        rows = []
        for s in p["lineup_now"]:
            after = s.get("after_moves") or ""
            if s["player"] is None:
                if not p.get("games_today", True):
                    continue                      # no games: the roster, not 17 empty slots
                rows.append(((s["slot"], "(empty)", "", "", "", "", "", "", after, *self._stats(None)),
                             ("plan",) if after else ()))
                continue
            tags = tuple(t for t in (s.get("flag"),) if t in STATUS_COLOURS)
            rows.append(((s["slot"], s["player"], _num(s["mean"]), _num(s["sd"]),
                          "" if s["p_plays"] is None else f"{s['p_plays']:.0%}", local_time(s["puck_utc"]),
                          s["flag"] or "", "\U0001f512" if s.get("locked") else "", after,
                          *self._stats(s.get("stats"))), tags + (("plan",) if after else ())))
        for name, stats in zip(p["bench_now"], p.get("bench_now_stats") or [None] * len(p["bench_now"])):
            rows.append((("BN", name, "", "", "", "", "", "", "", *self._stats(stats)), ()))
        self.tonight.set_rows(rows)
        # Tonight is always the per-game projection; the other tables say what their stats are.
        basis = p.get("stats_basis", "")
        for table, title in ((self.roster, "Roster"), (self.free_agents, "Free agents"),
                             (self.opponent, "Matchup")):
            self.tabs.tab(self._tab_of(table), text=f"{title} ({basis})" if basis else title)

        rows = [(("IR: move to", name, "", "", "", ""), ()) for name in p["ir_to"]]
        rows += [(("IR: activate", name, "", "", "", ""), ()) for name in p["ir_off"]]
        for m in p["moves"]:
            note = f"reported {m['add_status']}" if m.get("add_status") not in (None, "ACTIVE") else ""
            rows.append(((m["kind"].capitalize(), m["add"], _num(m["add_rate"]), m["drop"] or "",
                          _num(m["drop_rate"]), note), ()))
        rows += [(("Waiver claim", c["claim"], "", c["drop"] or "", "", ""), ()) for c in p["claims"]]
        rows += [(("Drop", "", "", name, "", ""), ()) for name in p["other_drops"]]
        self.moves.set_rows(rows or [(("", "No moves today.", "", "", "", ""), ("empty",))])

        rows = []
        blank = lambda x, f="": "" if x is None else format(x, f)
        for o in p.get("options", []):
            tags = ("plan",) if o["in_plan"] else ()
            rows.append(((blank(o["rank"]), o["kind"], o["add"], _num(o["add_rate"]), blank(o["add_games"]),
                          o["drop"] or "(open spot)", _num(o["drop_rate"]), blank(o["drop_games"]),
                          blank(o["gain"], "+.1f"), blank(o["bar"], ".1f"), blank(o["edge"], "+.1f"),
                          o["note"]), tags))
        self.options.set_rows(rows or [(("", "", "No pickups priced.") + ("",) * 9, ())])

        rows = []
        for w in p.get("week_plan", []):
            day = pd.Timestamp(w["day"]).strftime("%a %b %d").replace(" 0", " ")
            if w["from"] != w["day"]:
                day += f" (from {pd.Timestamp(w['from']).strftime('%a')})"
            rows.append(((day, w["kind"], w["add"], _num(w["add_rate"]), w["add_games"],
                          w["drop"] or "(open spot)", _num(w["drop_rate"]), blank(w["drop_games"]),
                          f"{w['gain']:+.1f}", f"{w['bar']:.1f}", f"{w['edge']:+.1f}",
                          "for next week" if w.get("for_next_week") else "today" if w["today"] else "planned"),
                         ("plan",) if w["today"] else ()))
        empty = ("No rentals worth a move this week." if p.get("stream_mode") == "week"
                 else "Streaming decides a day at a time (strategy mode 'daily').")
        self.week.set_rows(rows or [(("", "", empty) + ("",) * 9, ("empty",))])

        self._fill_sorted("roster")
        self._fill_sorted("free_agents")

        self.matchup_var.set(f"Week {p['week']}: {p['team']} {p['my_week_points']:.1f}  vs  "
                             f"{opponent} {p['opponent_week_points']:.1f}\n"
                             f"P(win this week) {p['p_win']:.0%}  (z {p['matchup_z']:+.2f})")
        self.opponent.set_rows([((r["player"], r["positions"], _num(r["rate"]), r["games_left"],
                                  *self._stats(r.get("stats"))), ())
                                for r in sorted(p["opponent_roster"], key=lambda r: -r["rate"])])

        self.goalie_box.delete(0, "end")
        for g in p["goalies"]:
            self.goalie_box.insert("end", f"{g['player']}: {g['p_start']:.0%}" + (f"  ({g['note']})" if g["note"] else ""))
        self.watch_box.delete(0, "end")
        for w in p["watch"]:
            self.watch_box.insert("end", f"{w['player']}: {w['status']}" + (f" -- {w['note']}" if w["note"] else ""))
        self.problem_var.set("\n".join(p["problems"]))

    def _fill_players(self, table, players, where=False, **sort):
        rows = []
        for r in players:
            place = ("IR" if r["on_ir"] else "lineup" if r["in_lineup"] else "bench") if where else \
                    ("on waivers" if r["on_waivers"] else "")
            tags = ("plan",) if r.get("plan") else (r["status"],) if r["status"] in STATUS_COLOURS else ()
            cells = {"player": r["player"], "positions": r["positions"], "status": r["status"] or "",
                     "rate": _num(r["rate"]), "ros_points": _num(r.get("ros_points"), 1),
                     "per_game": _num(r["per_game"]),
                     "plays_tonight": "" if r["plays_tonight"] is None else f"{r['plays_tonight']:.0%}",
                     "games_left": r["games_left"], "where": place, "plan": _action(r.get("plan")),
                     **dict(zip(self.stat_keys, self._stats(r.get("stats"))))}
            rows.append((tuple(cells[k] for k in table.keys), tags))
        table.set_rows(rows, **sort)

    def _tab_of(self, table):
        """The notebook tab a table sits in (the Matchup's sits in frames inside its tab)."""
        widget = table.frame
        while widget.master is not self.tabs:
            widget = widget.master
        return widget

    def _stats(self, stats):
        return [_stat((stats or {}).get(k)) for k in self.stat_keys]

    def _sort(self, name, key):
        """A header click: the same column flips the order; a new one starts high-first for
        numbers, A-Z for text."""
        state = self.sorts[name]
        state[1] = not state[1] if key == state[0] else key in (
            "rate", "ros_points", "per_game", "plays_tonight", "games_left", *self.stat_keys)
        state[0] = key
        self._fill_sorted(name)

    def _fill_sorted(self, name):
        """The Roster or Free agents table, in its current sort."""
        if not self.plan:
            return
        table, players = getattr(self, name), self.plan[name]
        key, descending = self.sorts[name]
        if key is not None:
            text = key in ("player", "positions", "status", "plan", "where")
            value = ((lambda r: (r.get("stats") or {}).get(key)) if key in self.stat_keys
                     else (lambda r: r[key]) if key != "where" else (lambda r: r["player"]))
            # Blanks last either way: a goalie has no hits, a player with no games no line.
            present = [r for r in players if value(r) is not None]
            players = (sorted(present, key=(lambda r: value(r) or "") if text else value,
                              reverse=descending)
                       + [r for r in players if value(r) is None])
        self._fill_players(table, players, where=name == "roster", sort_key=key,
                           descending=descending)

    def _show_freshness(self):
        lines = [f"{step}: {when:%I:%M %p}".replace(" 0", " ") for step, when in self.last.items()]
        if self.args.skip_snapshots:
            lines.append("reports: from the scheduled snapshots (--skip-snapshots)")
        if self.args.league_file:
            lines.append(f"league: {self.args.league_file} (not the platform)")
        self.fresh_var.set("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--league", default=leagues.DEFAULT_LEAGUE, help="A league in Settings/leagues/")
    parser.add_argument("--date", default=None, help="Game date (default: today on this PC)")
    parser.add_argument("--league-file", default=None, help="A league snapshot JSON instead of the platform")
    parser.add_argument("--platform-season", type=int, default=None, help="Read a past season's league (rehearsal)")
    parser.add_argument("--now", default=None, help="UTC moment for the per-game lock (default: now; rehearsal)")
    parser.add_argument("--no-auto", action="store_true", help="Start with the auto window off")
    parser.add_argument("--skip-snapshots", action="store_true",
                        help="Use the scheduled snapshots instead of taking fresh ones (faster)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    root = tk.Tk()
    PlanWindow(root, args)
    root.mainloop()


if __name__ == "__main__":
    main()

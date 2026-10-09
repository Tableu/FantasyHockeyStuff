#!/usr/bin/env python
"""The daily plan, in a window -- the in-season counterpart of draft_gui.py, one league per run.

    python plan_gui.py                              # the plan server at 127.0.0.1:8000, league beagles
    python plan_gui.py --server http://host:8000
    python plan_gui.py --league <name>              # a server planning that league

The window is the plan server's (Server/server.py) and runs none of the steps itself (planpass.py,
the server's refreshes). The day, the league's source and rehearsal options (--date,
--league-file, --now, --skip-snapshots) are the server's too.

    Full refresh    fresh injury, line-chart and goalie reports -> tonight's projections -> read the
                    league from its platform -> the shipped manager's plan
    Quick refresh   injury and goalie reports -> tonight's projections -> the plan again (lineup
                    news, late scratches) -- what the auto window runs
    Auto window     the server's: a quick refresh about 30 minutes before each group of games
                    (Fleaflicker locks each player at his own game), once per group

On opening the window shows the server's newest plan (a full refresh there when it has none
today); its buttons start the server's refreshes and stream their progress; every minute it looks
at the server and follows a refresh the server runs, or picks up a newer plan. The server saves
every plan as reports/<league>/plans/plan_{date}_{time}.md + .json. Recommend-only: make the moves
on the platform yourself.

    Tonight      tonight's lineup from the roster you hold now: expected points, chance he plays /
                 starts, puck time (local), injury / GTD flag and a lock once his game has started;
                 then his projected stat line
                 for tonight (per game, in the league's scored stats); then the bench and the IR
                 players, each with his report
    AI Suggestions  the model's recommendations, three sections one above the other (drag
                 between them to resize):
                 Today's moves  IR moves, adds and drops, claims -- with the rate each was priced on
                 Upgrades  permanent pickups: the plan's upgrades and claims, highlighted, above the
                           add/drop rule's own pricing of the top free agents on the roster you hold
                           now, each with his best drop, the lineup points he gains over the pricing
                           window, the bar a move must clear and the edge (gain - bar) they are ranked
                           by. Above them, OK to drop for upgrades: tick players and upgrades drop
                           only them -- goalies and starters included, even when that leaves a lineup
                           slot empty (roster.fill_check = none); none ticked, the model chooses. The
                           list stays open while you tick; clicking outside it saves and re-plans.
                           Forced drops (an IR activation into a full roster) stay the model's
                 Week plans  the week's streaming plans (strategy mode 'week', Decisions/weekplan.py)
                           by NHL team (TeamPlans): each rental is a team slot -- day, team,
                           position -- that any of the players on that team who fit it can fill. One
                           row per plan: its schedule ("Tue NYR RW -> Thu CGY C ..."), what it adds
                           this week, its expected edge (each slot's options discounted by the chance
                           each is taken first) and its thinnest slot -- planned after today's
                           upgrades, on the roster and moves they leave. Plan A is the model's best
                           week -- its moves today are among Today's moves; B, C, ... each leave out
                           every earlier plan's first team -- the fallbacks when a team is picked
                           over. Click a plan for its slots, a slot for its options, ranked by edge,
                           and an option to pick him with the plan's drop (a rental the plan picks up
                           earlier is picked with him) into your Weekly planner tab; a picked one (blue) to
                           take it back. The later days are planned again on every run
    Weekly planner  your week, day by day -- yours to build, apart from the model: your picks never
                 change its suggestions (the user, 2026-10-08). Each day a column: its lineup points
                 and open slots, your picks that day, and the league's lineup slots with who starts
                 in each that night -- a rental in every slot he fills while held (blue, open slots
                 red). The window builds it from your picks itself (Live/weekbook.py, on the plan's
                 workbench: your roster and moves as they stand, before the model's upgrades), so a
                 pick shows at once -- no re-plan. Click an open slot (+) or a
                 rental's (\u21c4) for the slot picker: the free agents who fit in that night's
                 lineup (any slot), with a position filter -- the plans' picks and options that day first (blue, which plans beside
                 them), then the most points that night -- with the week's lineup points each adds;
                 and beside them who to drop for the one selected (the week gain with each; the
                 plan's drop chosen for a plan's player; an open spot while the roster has room; a
                 rental's slot drops that rental). A cross takes a pick out; a pick that cannot go in
                 (no room, his drop not held then, over the move limit) says why in red. Clear
                 forgets the week's picks. Picks are saved on the server (Live/choices.py) at once,
                 for the window only
                 On the week's last day (Sunday) a switch, on the Weekly planner tab and above the week plans,
                 shows next week instead: planned from its first day on the roster today's moves
                 leave, with a fresh move limit -- a preview, planned again once it starts
    Roster       every player you hold now: status, rate, rest-of-season points, Periph % (the share
                 of his projected points from hits, blocks, shots and PIM: high = steady, low = a
                 volatile scorer), games left this week and his stats (sortable); injured players
                 coloured by status
    Free agents  the best available now by rate, with rest-of-season points (rate x his team's games
                 left in the fantasy season; sortable); injured players coloured by status

Roster, Free agents and the Matchup's opponent show each player's stats in the league's scored
categories: the season so far once it has started (ModelFeatures/build_season_stats.py and the goalie
starts, both nightly), the projected season before that -- the tab title says which.

The tables show the league as it stands; the moves are only recommendations until you make them.
    Matchup      the week so far, P(win), the opponent's roster

Tonight's games, live from the server (Server/games.py; the NHL's feeds, held in its memory), checked
every 5 s while one of these tabs is shown:
    Games        today's games -- score, clock, how many of your players and your opponent's are in
                 each; click one for its box score (fantasy points under the league's scoring, your
                 players and your opponent's coloured), its plays (newest first, filterable), its team
                 stats and its score and shots by period
    Goals        every goal today, newest first: scorer, assists, strength, the new score and the
                 fantasy points it earned either side, with tonight's totals; the highlight clip
                 opens in the browser
    Lines        a game's forward lines, defence pairs and power-play / penalty-kill units as they are
                 being used, from the NHL's time-on-ice reports (about a minute behind): the last 10
                 minutes of 5v5 or the game so far
"""

import argparse
import copy
import datetime as dt
import json
import logging
import queue
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from tkinter import ttk

import pandas as pd

import seasonlayer  # noqa: F401 -- puts Season/ on sys.path; see seasonlayer.py
import draft_board
import league as league_module
import leagues
import sheets
import simlayer
import weekbook

WATCH_MS = 60_000               # how often the window looks at the server
JOB_POLL_S = 1                  # how often a running refresh's progress is fetched
SERVER_TIMEOUT_S = 30
DEFAULT_SERVER = "http://127.0.0.1:8000"
LIVE_POLL_MS = 5_000            # the Games / Goals / Lines tabs, while shown
LINES_POLL_S = 15               # the lines change about once a minute (the TOI reports)
OWNER_COLOURS = {"me": "#d1fae5", "opp": "#ede9fe"}     # your players' rows, your opponent's
PLAY_FILTERS = ("All", "Goals", "Penalties", "Shots", "Your players", "Opponent's players")
SHOT_TYPES = {"goal", "shot-on-goal", "missed-shot", "blocked-shot"}
PLAN_COLOUR = "#e0ecff"         # a row the plan recommends acting on
STATUS_COLOURS = {"OUT": "#fde2e2", "SUSP": "#fde2e2", "DTD": "#fff4d6", "GTD": "#fff4d6"}
# A row's look by its tags: a recommended action, an injury status, greyed (a placeholder message).
# The Weekly planner tab's calendar: a cell's width in characters, and its colour: held, your rental, open.
CALENDAR_CELL_CHARS = 22
CALENDAR_COLOURS = {"held": "#f9fafb", "pinned": "#bfdbfe", "open": "#fde2e2"}
ROW_STYLES = {"plan": {"bg": PLAN_COLOUR}, "empty": {"fg": "#9ca3af"},
              **{status: {"bg": colour} for status, colour in STATUS_COLOURS.items()},
              **{owner: {"bg": colour} for owner, colour in OWNER_COLOURS.items()}}


# The slot picker's position filter, in this order (the positions its players have only).
POSITION_ORDER = ("C", "LW", "RW", "D", "G")

# Columns the Roster and Free agents tabs leave out of the shared player columns.
ROSTER_HIDDEN = {"per_game", "plays_tonight", "plan", "where"}
FREE_AGENTS_HIDDEN = {"plan", "where"}


def local_time(utc_text) -> str:
    """'2026-09-29 23:00:00' (UTC) -> '4:00 PM' on this PC's clock."""
    if not utc_text or utc_text in ("None", "NaT"):
        return ""
    stamp = pd.Timestamp(utc_text).tz_localize("UTC").tz_convert(dt.datetime.now().astimezone().tzinfo)
    return stamp.strftime("%I:%M %p").lstrip("0")


def clock(when: dt.datetime) -> str:
    return when.strftime("%I:%M %p").lstrip("0")


class Server:
    """The plan server (Server/server.py) over HTTP, for one of the leagues it plans."""

    def __init__(self, url, league):
        self.url, self.league = url.rstrip("/"), league

    def _call(self, path, body=None, method=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        path += ("&" if "?" in path else "?") + "league=" + urllib.parse.quote(self.league)
        request = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"} if data else {})
        with urllib.request.urlopen(request, timeout=SERVER_TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))

    def plan(self):
        """{file, saved_at, plan}: the day's newest saved plan, or None before the first."""
        try:
            return self._call("/plan")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise

    def status(self):
        return self._call("/status")

    def refresh(self, mode):
        """'full' / 'quick': every league; 'plan': this one again on its last read."""
        return self._call("/refresh", {"mode": mode})

    def job(self, job_id, after=0):
        return self._call(f"/jobs/{job_id}?after={after}")

    def choices(self):
        """What you chose (Live/choices.py): {upgrade_drops, days: {date: {drops, moves}}}."""
        return self._call("/choices")["choices"]

    def set_choices(self, choices):
        """Save them (nothing re-planned until a refresh or a 'plan'): {league, choices}."""
        return self._call("/choices", {"choices": choices}, method="PUT")

    def games(self):
        return self._call("/games")

    def goals(self):
        return self._call("/goals")

    def game(self, game_id, after=-1):
        return self._call(f"/games/{game_id}?after={after}")

    def lines(self, game_id):
        return self._call(f"/games/{game_id}/lines")


def period_name(number, kind=None) -> str:
    return {"OT": "OT", "SO": "SO"}.get(kind, f"P{number}" if number else "")


def game_state(g) -> str:
    """'Final', 'Final/OT', 'P2 12:34', 'P2 intermission' or the puck time (local)."""
    if g["state"] in ("OFF", "FINAL"):
        return "Final" + (f"/{g['period_type']}" if g.get("period_type") in ("OT", "SO") else "")
    if g["state"] in ("LIVE", "CRIT"):
        period = period_name(g.get("period"), g.get("period_type"))
        return f"{period} intermission" if g.get("intermission") else f"{period} {g.get('clock') or ''}".strip()
    return local_time(g["start_utc"].replace("T", " ").replace("Z", ""))


def owner_tags(owners) -> tuple:
    return tuple(o for o in ("me", "opp") if o in owners)


def _action(plan):
    """A row's recommended action: 'drop', 'move to IR', ... or an add's kind (upgrade, rental)."""
    if not plan:
        return ""
    return plan[0].upper() + plan[1:] if plan in ("drop", "move to IR", "activate", "claim") else f"Add ({plan})"


def _stat(x):
    """A stat cell: per-game projections to two decimals, season counts as whole numbers."""
    return "" if x is None else f"{x:.2f}" if isinstance(x, float) else str(x)


def _pct(x):
    """A share as a whole percent ('' when unknown: a goalie, or nobody projects him)."""
    return "" if x is None or (isinstance(x, float) and pd.isna(x)) else f"{x:.0%}"


def _num(x, digits=2):
    return "" if x is None or (isinstance(x, float) and pd.isna(x)) else f"{x:.{digits}f}"


class PlanWindow:
    def __init__(self, root, args):
        self.root, self.args = root, args
        self.league = leagues.load(args.league)
        self.server = Server(args.server, self.league.name)
        self.served = {}                   # the server's last /status and the shown plan's file
        self.live = {"busy": False, "again": False, "lines_at": 0.0, "error": None, "games": None}
        self.game_id = None                # the game the Games and Lines tabs show
        self.plays, self.plays_game, self.plays_last = [], None, -1
        self.goal_rows = []
        self.watching = False              # a /status look under way
        self.plan = None
        self.busy = False
        self.messages = queue.Queue()
        # Sortable tables: name -> [column key, descending]. The roster keeps the plan's order until
        # a header is clicked; the free agents start best rate first.
        self.sorts = {"roster": [None, False], "free_agents": ["rate", True]}
        # What you chose (Live/choices.py; the server keeps them): who upgrades may drop (AI
        # Suggestions) and the Weekly planner tab's picks.
        self.choices = {"upgrade_drops": [], "days": {}}
        self.drop_popup = None             # an open OK-to-drop list: (window, close)

        root.title(f"Plan -- {self.league.name}: {self.league.team_name or 'my team'}"
                   + f" (server {self.server.url})")
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
        # The calendar's rows: the league's lineup slots in its own order (C C LW LW ... G G); the
        # lineup is solved on the solver's order (Live/weekbook.py, as the server does).
        config = league_module.load(self.league.rules)
        self.slot_rows = [slot for slot, count in config.active_slots.items() for _ in range(count)]
        self.slot_order, self.accepts = config.slot_order(), config.accepts
        self.book = None                   # your week shown on the Weekly planner tab (weekbook.Week), or None
        scored = set(scoring.skaters) | set(scoring.goalies)
        self.stat_keys = [k for k in draft_board.STAT_HEADINGS if k == "gp" or k in scored]
        stat_columns = [(k, draft_board.STAT_HEADINGS[k], 48) for k in self.stat_keys]
        self.tonight = self._table("Tonight", [("slot", "Slot", 60), ("player", "Player", 240),
                                               ("mean", "Exp. pts", 80), ("sd", "SD", 60),
                                               ("p", "P(plays/starts)", 110), ("puck", "Puck", 90),
                                               ("flag", "Flag", 70), ("lock", "", 40)] + stat_columns)
        # The AI Suggestions tab: today's moves, the upgrades and the week's plans, one above the
        # other (`_build_suggestions`).
        moves_pane, upgrade_pane, plans_pane = self._build_suggestions()
        self.moves = sheets.Table(moves_pane, [("kind", "Move", 110), ("add", "Add", 260), ("add_rate", "pts/g", 70),
                                               ("drop", "Drop", 260), ("drop_rate", "pts/g", 70), ("note", "Note", 200)],
                                  ROW_STYLES)
        self.options = sheets.Table(upgrade_pane, [("rank", "#", 36), ("kind", "Move", 90), ("add", "Add", 230),
                                                   ("add_rate", "pts/g", 60), ("add_periph", "Periph", 60),
                                                   ("add_games", "Games", 60),
                                                   ("drop", "Drop", 230), ("drop_rate", "pts/g", 60),
                                                   ("drop_games", "Games", 60), ("gain", "Gain", 70),
                                                   ("bar", "Bar", 60), ("edge", "Edge", 60), ("note", "", 150)],
                                    ROW_STYLES)
        # Plans and slots opened in the week's plans, this week's and next week's apart:
        # ("plan", label) and ("slot", label, index). Plan A starts open.
        self.week_open = {"this": {("plan", "A")}, "next": {("plan", "A")}}
        self.week_rows = []                # plans row -> its plan's, slot's or option's key, or None
        self.week = sheets.Table(plans_pane, [("plan", "Plan", 70), ("day", "Day", 110),
                                         ("until", "Dropped", 70), ("team", "Team", 50),
                                         ("pos", "Pos", 60), ("kind", "Move", 90), ("add", "Add", 300),
                                         ("add_rate", "pts/g", 60), ("add_periph", "Periph", 60),
                                         ("add_games", "Games", 60),
                                         ("drop", "Drop", 230), ("drop_rate", "pts/g", 60),
                                         ("drop_games", "Games", 60), ("gain", "Gain", 70),
                                         ("bar", "Bar", 60), ("edge", "Edge", 60),
                                         ("expected", "Exp.", 60), ("depth", "Options", 65)],
                                 ROW_STYLES, on_row_click=self._toggle_week_plan)
        self._build_week()
        player_columns = [("player", "Player", 240), ("positions", "Pos", 80), ("status", "Status", 70),
                          ("rate", "Rate (pts/g)", 90), ("ros_points", "ROS pts", 70),
                          ("peripheral", "Periph %", 70),
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
        self._build_games()
        self._build_goals()
        self._build_lines()
        self.tabs.bind("<<NotebookTabChanged>>", lambda event: self._live_fetch())
        body.add(self._build_sidebar(body), weight=1)

        root.after(200, self._drain)
        root.after(LIVE_POLL_MS, self._live_tick)
        self._open()

    # ---------- layout ----------

    def _build_toolbar(self):
        bar = ttk.Frame(self.root, padding=(8, 8, 8, 4))
        bar.pack(fill="x")
        self.headline = tk.StringVar(value="Starting...")
        ttk.Label(bar, textvariable=self.headline, style="Big.TLabel").pack(side="left")
        self.auto_var = tk.StringVar(value="Auto: the server's")
        ttk.Label(bar, textvariable=self.auto_var).pack(side="right", padx=8)
        self.quick_button = ttk.Button(bar, text="Quick refresh", command=lambda: self.run("quick"))
        self.quick_button.pack(side="right", padx=4)
        self.full_button = ttk.Button(bar, text="Full refresh", command=lambda: self.run("full"))
        self.full_button.pack(side="right", padx=4)
        self.status_var = tk.StringVar()
        ttk.Label(bar, textvariable=self.status_var).pack(side="right", padx=12)

    def _table(self, title, columns, sort_as=None, on_row_click=None):
        frame = ttk.Frame(self.tabs)
        self.tabs.add(frame, text=title)
        on_sort = None if sort_as is None else (lambda key: self._sort(sort_as, key))
        return sheets.Table(frame, columns, ROW_STYLES, on_sort=on_sort, on_row_click=on_row_click)

    def _build_suggestions(self):
        """The AI Suggestions tab: the model's recommendations, three sections one above the other
        (drag between them to resize) -- today's moves; the upgrades, under the upgrades' drop list
        (a dropdown of the roster, a Clear button and Re-plan; the list stays open while you tick,
        and clicking outside it saves and re-plans, `_drop_popup`); and the week's plans, under a
        this week / next week switch. Returns the three sections' table frames."""
        frame = ttk.Frame(self.tabs)
        self.tabs.add(frame, text="AI Suggestions")
        split = ttk.PanedWindow(frame, orient="vertical")
        split.pack(fill="both", expand=True)

        def pane(title, weight):
            box = ttk.Frame(split, padding=(0, 4, 0, 0))
            split.add(box, weight=weight)
            bar = ttk.Frame(box, padding=(0, 2, 0, 4))
            bar.pack(fill="x")
            ttk.Label(bar, text=title, font=("Segoe UI", 10, "bold")).pack(side="left", padx=(4, 10))
            table = ttk.Frame(box)
            table.pack(fill="both", expand=True)
            return bar, table

        bar, moves = pane("Today's moves", 1)
        bar, upgrades = pane("Upgrades", 2)
        self.upgrade_menu_button = ttk.Button(bar, text="OK to drop for upgrades \u25be",
                                              command=self._open_upgrade_drops)
        self.upgrade_menu_button.pack(side="left", padx=6)
        self.upgrade_drops_var = tk.StringVar()
        ttk.Label(bar, textvariable=self.upgrade_drops_var).pack(side="left", padx=6)
        self.upgrade_clear = ttk.Button(bar, text="Clear (let the model choose)",
                                        command=lambda: self._save_choices({**self.choices, "upgrade_drops": []}))
        self.upgrade_clear.pack(side="left", padx=6)
        self.upgrade_replan = ttk.Button(bar, text="Re-plan", command=lambda: self.run("plan"))
        self.upgrade_replan.pack(side="left", padx=6)
        bar, plans = pane("Week plans", 3)
        self.week_which = tk.StringVar(value="this")
        self.week_switches = []            # (bar, [this week, next week]) -- here and on the Weekly planner tab
        self._week_switch(bar)
        ttk.Label(bar, text="Click a plan for its slots, a slot for its options, an option to pick him "
                            "into your Weekly planner tab.", foreground="#6b7280").pack(side="right", padx=6)

        def place_sashes(event):           # once laid out: the moves short, the plans the most room
            height = split.winfo_height()
            if height > 400:
                split.sashpos(0, 150)
                split.sashpos(1, 150 + (height - 150) * 2 // 5)
                split.unbind("<Configure>")
        split.bind("<Configure>", place_sashes)
        return moves, upgrades, plans

    def _week_switch(self, bar):
        """A this week / next week switch in `bar` (shown only on the week's last day, when the
        plan has next week's), on the shared `week_which`."""
        buttons = [ttk.Radiobutton(bar, value=value, variable=self.week_which, command=self._fill_week)
                   for value in ("this", "next")]
        self.week_switches.append((bar, buttons))

    def _fill_upgrade_menu(self):
        """The upgrades' drop list's button and the line beside it."""
        marked = set(self.choices.get("upgrade_drops", []))
        self.upgrade_drops_var.set(
            (f"{len(marked)} marked: upgrades drop only these." if marked else
             "None marked: the plan chooses who upgrades drop.")
            + (" Not planned yet -- press Re-plan." if self._unplanned() else ""))
        self.upgrade_clear.state(["!disabled"] if marked else ["disabled"])
        self.upgrade_menu_button.state(["disabled"] if self.busy else ["!disabled"])
        self.upgrade_replan.state(["disabled"] if self.busy else ["!disabled"])

    def _unplanned(self) -> bool:
        """Whether the upgrades' drop list saved differs from the one the plan shown was made on.
        (The Weekly planner tab's picks need no plan: the window builds your week itself.)"""
        made = (self.plan or {}).get("choices") or {}
        return (made.get("upgrade_drops") or []) != (self.choices.get("upgrade_drops") or [])

    def _open_upgrade_drops(self):
        """Who upgrades may drop: everyone on the roster (not IR)."""
        players = [{"player_id": r["player_id"], "player": r["player"]}
                   for r in (self.plan or {}).get("roster", []) if not r.get("on_ir") and r.get("player_id")]

        def apply(chosen):
            self._save_choices({**self.choices, "upgrade_drops": sorted(chosen)}, replan=True)

        self._drop_popup(self.upgrade_menu_button, "OK to drop for upgrades", players,
                         set(self.choices.get("upgrade_drops", [])), apply)

    def _drop_popup(self, anchor, title, players, marked, apply):
        """An OK-to-drop list under `anchor` that stays open while you tick: a box per player,
        ticked if `marked`. Clicking outside it (or Esc) closes it, and if anything changed calls
        `apply(the ticked ids)` -- which saves and re-plans."""
        if self.busy or not players:
            return
        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.geometry(f"+{anchor.winfo_rootx()}+{anchor.winfo_rooty() + anchor.winfo_height()}")
        box = tk.Frame(top, background="white", borderwidth=1, relief="solid")
        box.pack(fill="both", expand=True)
        tk.Label(box, text=f"{title} -- tick any, click outside to apply", background="white",
                 foreground="#6b7280", font=("Segoe UI", 8), anchor="w").pack(fill="x", padx=6, pady=(4, 2))
        ticks = {}
        for h in sorted(players, key=lambda h: h["player"]):
            ticks[h["player_id"]] = var = tk.BooleanVar(value=h["player_id"] in marked)
            tk.Checkbutton(box, text=h["player"], variable=var, background="white", anchor="w",
                           activebackground="#e0ecff").pack(fill="x", padx=6)

        def close(event=None):
            if not top.winfo_exists():
                return
            chosen = {q for q, var in ticks.items() if var.get()}
            self.root.unbind_all("<Escape>")
            top.grab_release()
            top.destroy()
            self.drop_popup = None
            if chosen != set(marked):
                apply(chosen)

        def click(event):
            x, y = event.x_root - top.winfo_rootx(), event.y_root - top.winfo_rooty()
            if not (0 <= x < top.winfo_width() and 0 <= y < top.winfo_height()):
                close()

        top.bind("<Button-1>", click)
        # Esc anywhere in the window while the list is open (a borderless window may never get the
        # keyboard focus on Windows).
        self.root.bind_all("<Escape>", close)
        top.focus_force()
        top.grab_set()                     # every click in the window comes here: outside closes
        self.drop_popup = (top, close)     # (a test can reach it)

    def _build_week(self):
        """The Weekly planner tab: your calendar, under a this week / next week switch (`_week_switch`), the
        picks' note and a Clear button."""
        frame = ttk.Frame(self.tabs)
        self.tabs.add(frame, text="Weekly planner")
        bar = ttk.Frame(frame, padding=(0, 6, 0, 4))
        bar.pack(fill="x")
        self._week_switch(bar)
        self.week_clear = ttk.Button(bar, text="Clear this week's picks", command=self._clear_week)
        self.week_clear.pack(side="right", padx=6)
        self.week_note = tk.StringVar()
        ttk.Label(bar, textvariable=self.week_note).pack(side="right", padx=6)
        # The calendar view: a grid of labels in a frame that scrolls.
        self.calendar_outer = ttk.Frame(frame)
        self.calendar_outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(self.calendar_outer, highlightthickness=0, background="white")
        scroll_y = ttk.Scrollbar(self.calendar_outer, orient="vertical", command=canvas.yview)
        scroll_x = ttk.Scrollbar(self.calendar_outer, orient="horizontal", command=canvas.xview)
        canvas.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        scroll_y.pack(side="right", fill="y")
        scroll_x.pack(side="bottom", fill="x")
        canvas.pack(fill="both", expand=True)
        self.calendar = tk.Frame(canvas, background="white")
        canvas.create_window((0, 0), window=self.calendar, anchor="nw")
        self.calendar.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<MouseWheel>", lambda event: canvas.yview_scroll(-event.delta // 120, "units"))

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

    def _build_games(self):
        """Today's games on top; the clicked game's box score, plays, team stats and periods below."""
        self.games_frame = frame = ttk.Frame(self.tabs)
        self.tabs.add(frame, text="Games")
        split = ttk.PanedWindow(frame, orient="vertical")
        split.pack(fill="both", expand=True)
        top = ttk.Frame(split)
        split.add(top, weight=1)
        self.games_table = sheets.Table(top, [("away", "Away", 70), ("score", "Score", 70), ("home", "Home", 70),
                                              ("state", "Game", 130), ("sog", "Shots", 70),
                                              ("mine", "Yours", 60), ("opp", "Opponent's", 85)],
                                        ROW_STYLES, on_row_click=self._pick_game)
        bottom = ttk.Frame(split, padding=(0, 6, 0, 0))
        split.add(bottom, weight=3)
        def place_sash(event):                                # once laid out: room for 8 games, drag for more
            if split.winfo_height() > 300:
                split.sashpos(0, 200)
                split.unbind("<Configure>")
        split.bind("<Configure>", place_sash)
        self.game_var = tk.StringVar(value="Click a game for its box score, plays and team stats.")
        ttk.Label(bottom, textvariable=self.game_var, style="Big.TLabel").pack(anchor="w", pady=(0, 4))
        views = ttk.Notebook(bottom)
        views.pack(fill="both", expand=True)

        self.box_columns = [("number", "#", 36), ("player", "Player", 170),
                            ("position", "Pos", 40), ("fantasy", "Fan. pts", 65), ("goals", "G", 34),
                            ("assists", "A", 34), ("ppp", "PPP", 40), ("shp", "SHP", 40), ("shots", "SOG", 40),
                            ("hits", "Hits", 40), ("blocks", "Blk", 40), ("pim", "PIM", 40),
                            ("plus_minus", "+/-", 40), ("toi", "TOI", 55), ("saves", "SV", 40),
                            ("goals_against", "GA", 40), ("decision", "Dec", 40)]
        self.sorts["box"] = ["fantasy", True]
        # A box score tab per NHL team, away first, named for the team once a game is picked; a
        # header click sorts both.
        self.game_views, self.box_frames, self.box_tables, self.box_titles = views, [], [], []
        for side in ("Away", "Home"):
            frame = ttk.Frame(views)
            views.add(frame, text=f"{side} box score")
            title = tk.StringVar()
            ttk.Label(frame, textvariable=title, font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(4, 2))
            self.box_frames.append(frame)
            self.box_titles.append(title)
            self.box_tables.append(sheets.Table(frame, self.box_columns, ROW_STYLES,
                                                on_sort=lambda key: self._sort_box(key)))

        plays = ttk.Frame(views)
        views.add(plays, text="Plays")
        bar = ttk.Frame(plays, padding=(0, 4))
        bar.pack(fill="x")
        ttk.Label(bar, text="Show").pack(side="left")
        self.play_filter = tk.StringVar(value=PLAY_FILTERS[0])
        chooser = ttk.Combobox(bar, textvariable=self.play_filter, values=PLAY_FILTERS, state="readonly", width=20)
        chooser.pack(side="left", padx=6)
        chooser.bind("<<ComboboxSelected>>", lambda event: self._fill_plays())
        self.plays_table = sheets.Table(plays, [("period", "Per", 45), ("time", "Time", 55), ("team", "Team", 50),
                                                ("type", "Event", 110), ("note", "Detail", 460)], ROW_STYLES)

        stats = ttk.Frame(views)
        views.add(stats, text="Team stats")
        self.team_stats_table = sheets.Table(stats, [("stat", "", 150), ("away", "Away", 100), ("home", "Home", 100)],
                                             ROW_STYLES)
        periods = ttk.Frame(views)
        views.add(periods, text="By period")
        self.periods_table = sheets.Table(periods, [("period", "Period", 70), ("away_goals", "Away goals", 85),
                                                    ("home_goals", "Home goals", 85), ("away_shots", "Away shots", 85),
                                                    ("home_shots", "Home shots", 85)], ROW_STYLES)

    def _build_goals(self):
        self.goals_frame = frame = ttk.Frame(self.tabs, padding=(0, 6, 0, 0))
        self.tabs.add(frame, text="Goals")
        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(0, 4))
        self.goals_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.goals_var, style="Big.TLabel").pack(side="left")
        ttk.Button(bar, text="Play highlight", command=self._play_clip).pack(side="right", padx=4)
        self.goals_fantasy_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Only goals with your players or your opponent's",
                        variable=self.goals_fantasy_only, command=self._fill_goals).pack(side="right", padx=8)
        self.goals_table = sheets.Table(frame, [("at", "At", 70), ("time", "Game", 70), ("team", "Team", 45),
                                                ("player", "Scorer", 150),
                                                ("note", "Assists", 215), ("strength", "Str", 45),
                                                ("score", "Score", 105), ("fantasy", "Fantasy points", 200),
                                                ("clip", "Clip", 40)],
                                        ROW_STYLES, on_row_click=self._pick_goal)
        self.goal_picked = None

    def _build_lines(self):
        self.lines_frame = frame = ttk.Frame(self.tabs, padding=(0, 6, 0, 0))
        self.tabs.add(frame, text="Lines")
        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(0, 4))
        ttk.Label(bar, text="Game").pack(side="left")
        self.lines_game = tk.StringVar()
        self.lines_chooser = ttk.Combobox(bar, textvariable=self.lines_game, state="readonly", width=34)
        self.lines_chooser.pack(side="left", padx=6)
        self.lines_chooser.bind("<<ComboboxSelected>>", lambda event: self._pick_lines_game())
        self.lines_window = tk.StringVar(value="recent")
        for value, text in (("recent", "Last 10 min of 5v5"), ("game", "Game so far")):
            ttk.Radiobutton(bar, text=text, value=value, variable=self.lines_window,
                            command=self._fill_lines).pack(side="left", padx=6)
        self.lines_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.lines_var).pack(side="left", padx=12)
        sides = ttk.Frame(frame)
        sides.pack(fill="both", expand=True)
        self.lines_tables, self.lines_titles = [], []
        for row in range(2):                    # away above, home below: a unit's names need the width
            side = ttk.Frame(sides, padding=(0, 0 if row == 0 else 6, 0, 0))
            side.grid(row=row, column=0, sticky="nsew")
            sides.rowconfigure(row, weight=1)
            title = tk.StringVar()
            ttk.Label(side, textvariable=title, font=("Segoe UI", 10, "bold")).pack(anchor="w")
            self.lines_titles.append(title)
            self.lines_tables.append(sheets.Table(side, [("unit", "Unit", 45), ("player", "Players", 470),
                                                         ("shared", "Together", 70), ("note", "Each", 270)],
                                                  ROW_STYLES))
        sides.columnconfigure(0, weight=1)
        self.lines_data = None

    def _build_sidebar(self, parent):
        side = ttk.Frame(parent, padding=(8, 0, 0, 0))
        self.problem_var = tk.StringVar()
        ttk.Label(side, textvariable=self.problem_var, style="Warn.TLabel", wraplength=320).pack(anchor="w", pady=(6, 0))
        self.fresh_var = tk.StringVar()
        ttk.Label(side, text="Data", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(8, 2))
        ttk.Label(side, textvariable=self.fresh_var, justify="left", wraplength=320).pack(anchor="w")
        ttk.Label(side, text="Progress", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(8, 2))
        self.log = tk.Text(side, height=14, width=46, font=("Consolas", 8), wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True)
        return side

    # ---------- the server's refreshes ----------

    def echo(self, text):
        self.messages.put(("log", f"{dt.datetime.now():%H:%M:%S}  {text}"))

    def run(self, mode):
        """Start a refresh on the server, followed on a worker thread: 'full', 'quick', or 'plan' --
        this league again on its last read and your choices (about 2 s)."""
        if self.busy:
            return
        self.busy = True
        self.full_button.state(["disabled"])
        self.quick_button.state(["disabled"])
        self.status_var.set({"full": "Full refresh running...", "quick": "Quick refresh running...",
                             "plan": "Re-planning on your choices..."}[mode])
        if self.plan:
            self._fill_upgrade_menu()          # its dropdown off until the plan comes back
        threading.Thread(target=self._work, args=(mode,), daemon=True).start()

    def _drain(self):
        while not self.messages.empty():
            kind, payload = self.messages.get()
            if kind == "log":
                self.log.configure(state="normal")
                self.log.insert("end", payload + "\n")
                self.log.see("end")
                self.log.configure(state="disabled")
            elif kind == "served":                # (/plan's answer, 'updated' or 'saved')
                body, how = payload
                self.plan, self.served["file"] = body["plan"], body["file"]
                if how == "updated":
                    self._finished(f"Plan updated {dt.datetime.now():%I:%M %p}".replace(" 0", " "))
                else:
                    saved = clock(dt.datetime.fromisoformat(body["saved_at"]))
                    self.status_var.set(f"Showing the server's plan saved at {saved}")
                    self._show_freshness()
                self.show()
            elif kind == "status":                # the server's /status
                self.served["status"] = payload
                self.auto_var.set("Auto: the server's (on)" if payload.get("auto") else "Auto: off on the server")
                self._show_freshness()
            elif kind == "follow":                # a refresh the server started on its own
                if not self.busy:
                    self.busy = True
                    self.full_button.state(["disabled"])
                    self.quick_button.state(["disabled"])
                    self.status_var.set("Re-planning on your choices..." if payload["by"] == "choices"
                                        else f"Server refresh running ({payload['by']})...")
                    if self.plan:
                        self._fill_upgrade_menu()  # its dropdown off until the plan comes back
                    threading.Thread(target=self._follow, args=(payload["id"],), daemon=True).start()
            elif kind == "choices":               # what the server saved of your choices
                self.choices = payload
                if self.plan:
                    self._fill_upgrade_menu()
                    self._fill_week()
            elif kind == "live":
                self._show_live(payload)
            elif kind == "start":
                self.run(payload)
            elif kind == "watched":
                self.watching = False
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
        if self.plan:
            self._fill_upgrade_menu()
            self._fill_week()

    def _open(self):
        """On opening: the server's newest plan, a refresh it is running followed, or a full
        refresh there when it has no plan today."""
        def work():
            try:
                try:
                    status = self.server.status()
                except urllib.error.HTTPError as error:      # 404: a league it does not plan
                    if error.code != 404:
                        raise
                    detail = json.loads(error.read().decode("utf-8") or "{}").get("detail", "")
                    raise RuntimeError(f"{detail}: open the window with --league and one of those") from None
                self.messages.put(("status", status))
                self.messages.put(("choices", self.server.choices()))
                body = self.server.plan()
                if body is not None:
                    self.messages.put(("served", (body, "saved")))
                if status["job"] is not None:
                    self.messages.put(("follow", status["job"]))
                elif body is None:
                    self.echo(f"the server has no plan for {status['day']} yet -- full refresh")
                    self.messages.put(("start", "full"))
            except Exception as error:  # noqa: BLE001 -- show it, keep the window
                self.messages.put(("error", f"server {self.server.url}: {type(error).__name__}: {error}"))
        self.status_var.set(f"Reading the server at {self.server.url}...")
        threading.Thread(target=work, daemon=True).start()
        self.root.after(WATCH_MS, self._watch)

    def _work(self, mode):
        try:
            job = self.server.refresh(mode)
            if job["mode"] != mode:
                self.echo(f"the server is already running a {job['mode']} refresh -- following it")
            self._follow(job["id"])
        except Exception as error:  # noqa: BLE001
            self.messages.put(("error", f"server {self.server.url}: {type(error).__name__}: {error}"))

    def _follow(self, job_id):
        """A server refresh's progress lines until it ends -- and any the server started straight
        after it (a re-plan on choices saved while it ran) -- then the plan (on a worker thread)."""
        try:
            after = 0
            while True:
                job = self.server.job(job_id, after)
                for line in job["lines"]:
                    self.messages.put(("log", line))
                after = job["next"]
                if job["state"] == "running":
                    time.sleep(JOB_POLL_S)
                    continue
                status = self.server.status()
                queued = status.get("job")
                if queued is None or queued["id"] == job_id:
                    break
                job_id, after = queued["id"], 0
            self.messages.put(("status", status))
            if job["state"] == "failed":
                self.messages.put(("error", f"server refresh failed: {job['error']}"))
            else:
                self.messages.put(("served", (self.server.plan(), "updated")))
        except Exception as error:  # noqa: BLE001
            self.messages.put(("error", f"server {self.server.url}: {type(error).__name__}: {error}"))

    def _watch(self):
        """Every minute: follow a refresh the server started (its auto window), or pick up a newer
        plan than the one shown."""
        def work():
            try:
                status = self.server.status()
                self.messages.put(("status", status))
                if status["job"] is not None:
                    self.messages.put(("follow", status["job"]))
                elif status["plan_file"] and status["plan_file"] != self.served.get("file"):
                    body = self.server.plan()
                    if body is not None:
                        self.messages.put(("served", (body, "saved")))
            except Exception as error:  # noqa: BLE001 -- a missed look; the next one tries again
                self.echo(f"server not reached: {type(error).__name__}: {error}")
            finally:
                self.messages.put(("watched", None))
        try:
            if not self.busy and not self.watching:
                self.watching = True
                threading.Thread(target=work, daemon=True).start()
        finally:
            self.root.after(WATCH_MS, self._watch)

    # ---------- live games: fetching ----------

    def _live_tick(self):
        try:
            self._live_fetch()
        finally:
            self.root.after(LIVE_POLL_MS, self._live_tick)

    def _live_fetch(self):
        """What the shown tab needs from the server, on a worker thread (one at a time)."""
        shown = self.tabs.select()
        frames = {str(self.games_frame): "games", str(self.goals_frame): "goals", str(self.lines_frame): "lines"}
        tab = frames.get(shown)
        if tab is None:
            return
        if self.live["busy"]:
            self.live["again"] = True         # e.g. a game clicked mid-fetch: fetch it when this one ends
            return
        want = {"games"}
        if tab == "goals":
            want.add("goals")
        if tab == "games" and self.game_id:
            want.add("game")
        if tab == "lines" and self.game_id and time.monotonic() - self.live["lines_at"] >= LINES_POLL_S:
            want.add("lines")
        after = self.plays_last if self.plays_game == self.game_id else -1
        self.live["busy"] = True
        threading.Thread(target=self._live_work, args=(want, self.game_id, after), daemon=True).start()

    def _live_work(self, want, game_id, after):
        out = {"game_id": game_id}
        try:
            out["games"] = self.server.games()
            if "goals" in want:
                out["goals"] = self.server.goals()
            if "game" in want:
                out["game"] = self.server.game(game_id, after)
                out["after"] = after
            if "lines" in want:
                out["lines"] = self.server.lines(game_id)
        except Exception as error:  # noqa: BLE001 -- shown once in Progress; the next tick tries again
            out["error"] = f"live games: {type(error).__name__}: {error}"
        self.messages.put(("live", out))

    def _show_live(self, out):
        self.live["busy"] = False
        if self.live["again"]:
            self.live["again"] = False
            self.root.after(0, self._live_fetch)
        error = out.get("error")
        if error and error != self.live["error"]:
            self.echo(error)
        self.live["error"] = error
        if "games" in out:
            self.live["games"] = out["games"]
            self._fill_games()
        if "goals" in out:
            self.goal_data = out["goals"]
            self._fill_goals()
        if "game" in out and out["game_id"] == self.game_id:
            self._show_game(out["game"], out["after"])
        if "lines" in out and out["game_id"] == self.game_id:
            self.live["lines_at"] = time.monotonic()
            self.lines_data = out["lines"]
            self._fill_lines()

    # ---------- live games: showing ----------

    @staticmethod
    def _set(table, rows, **sort):
        """Redraw only when something changed: a redraw every few seconds would jump the scroll."""
        key = (rows, tuple(sorted(sort.items())))
        if getattr(table, "shown_key", None) != key:
            table.shown_key = key
            table.set_rows(rows, **sort)

    def _fill_games(self):
        data = self.live["games"]
        rows, labels = [], []
        for g in data["games"]:
            started = g["state"] not in ("FUT", "PRE")
            rows.append(((g["away"]["abbrev"], f"{g['away']['score']} - {g['home']['score']}" if started else "",
                          g["home"]["abbrev"], game_state(g),
                          f"{g['away']['sog']} - {g['home']['sog']}" if started else "",
                          g["mine"] or "", g["opp"] or ""),
                         ("plan",) if g["id"] == self.game_id else ()))
            labels.append(f"{g['away']['abbrev']} @ {g['home']['abbrev']} -- {game_state(g)}")
        self._set(self.games_table, rows or [(("", "", "No NHL games today.", "", "", "", ""), ("empty",))])
        self.lines_chooser.configure(values=labels)
        ids = [g["id"] for g in data["games"]]
        if self.game_id in ids:
            self.lines_game.set(labels[ids.index(self.game_id)])

    def _pick_game(self, index):
        games = (self.live["games"] or {}).get("games", [])
        if index < len(games):
            self._choose_game(games[index]["id"])

    def _pick_lines_game(self):
        games = (self.live["games"] or {}).get("games", [])
        index = self.lines_chooser.current()
        if 0 <= index < len(games):
            self._choose_game(games[index]["id"])

    def _choose_game(self, game_id):
        if game_id != self.game_id:
            self.game_id, self.lines_data = game_id, None
            self.live["lines_at"] = 0.0
            self.game_var.set("Loading the game...")
            for table in self.lines_tables:
                self._set(table, [])
            self.lines_var.set("")
            self._fill_games()
        self._live_fetch()

    def _show_game(self, game, after):
        if self.plays_game != game["id"] or after < 0:
            self.plays, self.plays_game = [], game["id"]
        self.plays = game["plays"] + self.plays
        self.plays_last = max(self.plays_last if after >= 0 else -1, game["last_sort"])
        self.game_data = game
        away, home = game["away"], game["home"]
        self.game_var.set(f"{away['abbrev']} {away['score']}  -  {home['score']} {home['abbrev']}    "
                          f"{game_state({**game, 'start_utc': ''})}    shots {away['sog']} - {home['sog']}")
        self._fill_box()
        self._fill_plays()
        labels = {"sog": "Shots on goal", "faceoffWinningPctg": "Faceoff %", "powerPlay": "Power play",
                  "powerPlayPctg": "Power play %", "pim": "Penalty minutes", "hits": "Hits",
                  "blockedShots": "Blocked shots", "giveaways": "Giveaways", "takeaways": "Takeaways",
                  "faceoffWins": "Faceoffs won"}
        pct = lambda c, v: f"{v:.0%}" if c.endswith("Pctg") and isinstance(v, (int, float)) else str(v)
        self._set(self.team_stats_table,
                  [((labels.get(t["category"], t["category"]), pct(t["category"], t["awayValue"]),
                     pct(t["category"], t["homeValue"])), ()) for t in game["team_stats"]])
        shots = {(p["periodDescriptor"]["number"]): p for p in game["shots_by_period"]}
        rows = []
        for p in game["linescore"]:
            number = p["periodDescriptor"]["number"]
            s = shots.get(number, {})
            rows.append(((period_name(number, p["periodDescriptor"].get("periodType")), p["away"], p["home"],
                          s.get("away", ""), s.get("home", "")), ()))
        self._set(self.periods_table, rows)

    def _sort_box(self, key):
        state = self.sorts["box"]
        state[1] = not state[1] if key == state[0] else key not in ("player", "position", "decision")
        state[0] = key
        self._fill_box()

    def _fill_box(self):
        game = getattr(self, "game_data", None)
        if not game:
            return
        key, descending = self.sorts["box"]
        field = "name" if key == "player" else key
        text = key in ("player", "position", "decision", "toi")
        value = (lambda p: str(p[field])) if text else (lambda p: p[field])
        for side, frame, title, table in zip(("away", "home"), self.box_frames, self.box_titles, self.box_tables):
            team = game[side]
            self.game_views.tab(frame, text=f"{team['abbrev']} box score")
            title.set(f"{team['abbrev']}  {team['score']}  ({team['sog']} shots)")
            present = [p for p in team["players"] if p.get(field) is not None]
            players = (sorted(present, key=value, reverse=descending)
                       + [p for p in team["players"] if p.get(field) is None])
            rows = []
            for p in players:
                cells = {**p, "player": p["name"]}
                rows.append((tuple("" if cells.get(k) is None else cells[k] for k, _, _ in self.box_columns),
                             owner_tags([p.get("owner")])))
            self._set(table, rows, sort_key=key, descending=descending)

    def _fill_plays(self):
        choice = self.play_filter.get()
        keep = {"Goals": lambda p: p["type"] == "goal",
                "Penalties": lambda p: p["type"] == "penalty",
                "Shots": lambda p: p["type"] in SHOT_TYPES,
                "Your players": lambda p: "me" in p["owners"],
                "Opponent's players": lambda p: "opp" in p["owners"]}.get(choice, lambda p: True)
        rows = [((period_name(p["period"], p.get("period_type")), p["time"], p["team"],
                  p["type"].replace("-", " "), p["text"]), owner_tags(p["owners"]))
                for p in self.plays if keep(p)]
        self._set(self.plays_table, rows)

    def _fill_goals(self):
        data = getattr(self, "goal_data", None)
        if not data:
            return
        totals = data["totals"]
        self.goals_var.set(f"Tonight's goals: {data['team']} +{totals['me']:g}    "
                           f"{data['opponent']} +{totals['opp']:g}")
        rows, self.goal_rows = [], []
        for g in data["goals"]:
            people = [g["scorer"]] + g["assists"]
            owners = [p["owner"] for p in people if p["owner"]]
            if self.goals_fantasy_only.get() and not owners:
                continue
            fantasy = []
            for owner, label in (("me", data["team"]), ("opp", data["opponent"])):
                mine = [p for p in people if p["owner"] == owner]
                if mine:
                    fantasy.append(f"{label} +{sum(p['points'] for p in mine):g} "
                                   f"({', '.join(p['name'] for p in mine)})")
            assists = ", ".join(f"{a['name']} ({a['to_date']})" for a in g["assists"]) or "unassisted"
            scorer = f"{g['scorer']['name']} ({g['scorer']['to_date']})"
            strength = g["strength"] + (" EN" if g.get("modifier") == "empty-net" else "")
            at = local_time(g["at"].replace("T", " ")[:19]) if g.get("at") else ""
            if at and g.get("at_estimated"):
                at = "~" + at                     # not seen arriving: estimated from puck drop
            rows.append(((at, f"{period_name(g['period'], g.get('period_type'))} {g['time']}", g["team"], scorer,
                          assists, strength, g["score"], "; ".join(fantasy), "\u25b6" if g.get("clip") else ""),
                         owner_tags(owners)))
            self.goal_rows.append(g)
        self._set(self.goals_table, rows or [(("", "", "", "No goals yet.", "", "", "", "", ""), ("empty",))])

    def _pick_goal(self, index):
        self.goal_picked = self.goal_rows[index] if index < len(self.goal_rows) else None

    def _play_clip(self):
        clip = (self.goal_picked or {}).get("clip")
        if clip:
            webbrowser.open(clip)
        else:
            self.echo("click a goal with a clip (\u25b6) first; the NHL posts each clip a few minutes after the goal")

    def _fill_lines(self):
        data = self.lines_data
        if not data:
            return
        window = self.lines_window.get()
        note = data.get("note")
        if note:
            self.lines_var.set(note)
        else:
            missing = data.get("missing_seconds") or 0
            self.lines_var.set(f"time-on-ice reports through {data.get('as_of')}"
                               + (f"; {missing // 60}:{missing % 60:02d} of play missing from them" if missing else ""))
        teams = data.get("teams") or []
        for i, table in enumerate(self.lines_tables):
            if i >= len(teams):
                self.lines_titles[i].set("")
                self._set(table, [])
                continue
            team = teams[i]
            w = team["windows"][window]
            self.lines_titles[i].set(f"{team['team']}  (" + ("the last " if window == "recent" else "")
                                     + f"{w['seconds'] // 60} min of 5v5" + (")" if window == "recent" else " so far)"))
            rows = []

            def add(label, units, empty):
                if not units:
                    rows.append(((label, empty, "", ""), ("empty",)))
                for u in units:
                    names = ", ".join(p["name"] + {"me": " (you)", "opp": " (opp)"}.get(p["owner"], "")
                                      for p in u["players"])
                    each = ", ".join(f"{p['toi'] // 60}:{p['toi'] % 60:02d}" for p in u["players"])
                    tags = ("empty",) if u["thin"] else owner_tags([p["owner"] for p in u["players"]])
                    rows.append(((f"{label}{u['rank']}", names + (" -- not enough ice yet" if u["thin"] else ""),
                                  f"{u['shared'] // 60}:{u['shared'] % 60:02d}", each), tags))

            add("F", w["forwards"], "no 5v5 yet")
            add("D", w["defence"], "no 5v5 yet")
            add("PP", team["pp"], f"under a minute of power play ({team['pp_seconds']} s)")
            add("PK", team["pk"], f"under a minute shorthanded ({team['sh_seconds']} s)")
            self._set(table, rows)

    # ---------- showing the plan ----------

    def show(self):
        p = self.plan
        opponent = p["opponent"] or "no opponent"
        self.headline.set(f"{p['team']} -- {p['game_date']} · week {p['week']} vs {opponent} · "
                          f"P(win) {p['p_win']:.0%} · moves left after the plan {p['moves_left']}"
                          + (" · today's moves are free" if p.get("free_moves") else "")
                          + (f" · {len(p.get('week_plan', []))} rental(s) planned this week"
                             if p.get("stream_mode") == "week" else "")
                          + ("" if p.get("games_today", True) else " · no NHL games today")
                          # The upgrades' drop list overrides the model's drops until cleared.
                          + (f" · upgrades drop only {len(p['choices']['upgrade_drops'])} marked player(s)"
                             if (p.get("choices") or {}).get("upgrade_drops") else ""))

        rows = []
        for s in p["lineup_now"]:
            if s["player"] is None:
                if not p.get("games_today", True):
                    continue                      # no games: the roster, not 17 empty slots
                rows.append(((s["slot"], "(empty)", "", "", "", "", "", "", *self._stats(None)), ()))
                continue
            tags = tuple(t for t in (s.get("flag"),) if t in STATUS_COLOURS)
            rows.append(((s["slot"], s["player"], _num(s["mean"]), _num(s["sd"]),
                          "" if s["p_plays"] is None else f"{s['p_plays']:.0%}", local_time(s["puck_utc"]),
                          s["flag"] or "", "\U0001f512" if s.get("locked") else "",
                          *self._stats(s.get("stats"))), tags))
        # Bench and IR players carry their report too (OUT, DTD, GTD...), from the roster you hold now.
        status = {r["player"]: r.get("status") for r in p.get("roster", [])}
        for name, stats in zip(p["bench_now"], p.get("bench_now_stats") or [None] * len(p["bench_now"])):
            flag = status.get(name)
            rows.append((("BN", name, "", "", "", "", flag or "", "", *self._stats(stats)),
                         (flag,) if flag in STATUS_COLOURS else ()))
        for r in p.get("roster", []):
            if r.get("on_ir"):
                flag = r.get("status")
                rows.append((("IR", r["player"], "", "", "", "", flag or "", "", *self._stats(None)),
                             (flag,) if flag in STATUS_COLOURS else ()))
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
            if p.get("moves_from"):           # an add after the day's first puck: from tomorrow
                start = pd.Timestamp(p["moves_from"]).strftime("%a")
                when = f"adds count from {start}" + (": drop after his game tonight" if m["drop"] else "")
                note = "; ".join(x for x in (when, note) if x)
            rows.append(((m["kind"].capitalize(), m["add"], _num(m["add_rate"]), m["drop"] or "",
                          _num(m["drop_rate"]), note), ()))
        rows += [(("Waiver claim", c["claim"], "", c["drop"] or "", "", c.get("note") or ""), ())
                 for c in p["claims"]]
        rows += [(("Drop", "", "", name, "", ""), ()) for name in p["other_drops"]]
        self.moves.set_rows(rows or [(("", "No moves today.", "", "", "", ""), ("empty",))])

        rows = []
        blank = lambda x, f="": "" if x is None else format(x, f)
        for o in p.get("options", []):
            tags = ("plan",) if o["in_plan"] else ()
            rows.append(((blank(o["rank"]), o["kind"], o["add"], _num(o["add_rate"]), _pct(o.get("add_periph")),
                          blank(o["add_games"]),
                          o["drop"] or "(open spot)", _num(o["drop_rate"]), blank(o["drop_games"]),
                          blank(o["gain"], "+.1f"), blank(o["bar"], ".1f"), blank(o["edge"], "+.1f"),
                          o["note"]), tags))
        self.options.set_rows(rows or [(("", "", "No pickups priced.") + ("",) * 10, ())])

        self._fill_upgrade_menu()
        self._fill_week()

        self._fill_sorted("roster")
        self._fill_sorted("free_agents")

        self.matchup_var.set(f"Week {p['week']}: {p['team']} {p['my_week_points']:.1f}  vs  "
                             f"{opponent} {p['opponent_week_points']:.1f}\n"
                             f"P(win this week) {p['p_win']:.0%}  (z {p['matchup_z']:+.2f})")
        self.opponent.set_rows([((r["player"], r["positions"], _num(r["rate"]), r["games_left"],
                                  *self._stats(r.get("stats"))), ())
                                for r in sorted(p["opponent_roster"], key=lambda r: -r["rate"])])

        self.problem_var.set("\n".join(p["problems"]))

    def _fill_week(self):
        """The week plans (AI Suggestions), three levels deep. A plan's row: its schedule (one team slot per move), what
        it adds this week and its expected edge, the teams it leaves out. Under a plan clicked open,
        its slots: day, team, position, the best option and its drop. Under a slot clicked open, the
        players on that team who fit it, ranked by edge -- any of them buys the same nights; click
        one to pick him with the plan's drop. Your picks are highlighted."""
        p = self.plan
        # Next week's plans exist on the week's last day only; any other day the switch is hidden
        # and this week shows.
        has_next = bool(p.get("next_week_plans"))
        for bar, buttons in self.week_switches:
            for button in buttons:
                button.pack_forget()
            if has_next:
                buttons[0].configure(text=f"This week (week {p['week']})")
                buttons[1].configure(text=f"Next week (week {p['next_week']})")
                for button in buttons:
                    button.pack(side="left", padx=6)
        if not has_next:
            self.week_which.set("this")
        which = self.week_which.get()
        self._build_book()
        self._fill_calendar()
        if which == "next":
            plans = p["next_week_plans"]
        else:
            plans = p.get("week_plans") or [{"label": "A", "first": None, "week_gain": None,
                                             "moves": p.get("week_plan", [])}]
        opened = self.week_open[which]
        rows, self.week_rows = [], []
        blank = lambda x, f="": "" if x is None else format(x, f)
        weekday = lambda d: pd.Timestamp(d).strftime("%a %b %d").replace(" 0", " ")
        arrow = lambda key: "\u25be" if key in opened else "\u25b8"

        for plan in plans:
            if not plan["moves"]:
                continue
            label, moves = plan["label"], plan["moves"]
            key = ("plan", label)
            without = plan.get("without") or []
            rows.append(((f"{arrow(key)} {label}", weekday(moves[0]["day"]), "", "", "",
                          f"{len(moves)} move{'s' if len(moves) != 1 else ''}",
                          " \u2192 ".join(plan.get("schedule") or [w["add"] for w in moves]), "", "",
                          blank(plan.get("games")),
                          f"without {', '.join(without)}" if without else "", "", "",
                          blank(plan["week_gain"], "+.1f"), "", blank(plan.get("week_edge"), "+.1f"),
                          blank(plan.get("expected"), "+.1f"), blank(plan.get("thinnest"))), ()))
            self.week_rows.append(key)
            if key not in opened:
                continue
            for index, w in enumerate(moves):
                picked = {m["add"] for m in self.choices.get("days", {}).get(w["day"], {}).get("moves", [])}
                made = w.get("add_id") in picked
                slot_key = ("slot", label, index)
                options = w.get("options") or []
                day = weekday(w["day"])
                if w["from"] != w["day"]:
                    day += f" (from {pd.Timestamp(w['from']).strftime('%a')})"
                # The day the plan drops him for its next rental: his Games stop there.
                until = pd.Timestamp(w["until"]).strftime("%a") if w.get("until") else ""
                rows.append(((f"   {arrow(slot_key) if options else ''}", day, until, w.get("team") or "",
                              w.get("pos") or "", w["kind"], w["add"], _num(w["add_rate"]),
                              _pct(w.get("add_periph")), w["add_games"],
                              w["drop"] or "(open spot)", _num(w["drop_rate"]), blank(w["drop_games"]),
                              f"{w['gain']:+.1f}", f"{w['bar']:.1f}", f"{w['edge']:+.1f}",
                              blank(w.get("expected"), "+.1f"), len(options) or ""),
                             ("plan",) if made else ()))
                self.week_rows.append(slot_key if options or w.get("near") else None)
                if slot_key not in opened:
                    continue
                # Other teams' near ties: not this slot's options (those are its team's), but
                # pickups that day the team slot hides, within a point of the pick.
                for n in w.get("near") or []:
                    rows.append((("", "", "", n.get("team") or "", n["positions"], "other team",
                                  f"      \u2248 {n['add']}", _num(n["add_rate"]), "", n["add_games"],
                                  "", "", "", "", "", f"{n['delta']:+.1f} vs pick", "", ""), ()))
                    self.week_rows.append(None)
                # Click an option to pick him that day, with the plan's drop (`_pick_from_plan`);
                # a picked one (blue) to take it back.
                for rank, o in enumerate(options, 1):
                    start = "" if o["from"] == w["from"] else f" (from {pd.Timestamp(o['from']).strftime('%a')})"
                    status = f" [{o['status']}]" if o.get("status") not in (None, "ACTIVE") else ""
                    tags = (("plan",) if o.get("player_id") in picked
                            else (o["status"],) if o.get("status") in STATUS_COLOURS else ())
                    rows.append((("", "", "", "", o["positions"], o["kind"], f"      {rank}. {o['add']}{status}{start}",
                                  _num(o["add_rate"]), _pct(o.get("add_periph")), o["add_games"],
                                  "", "", "", f"{o['gain']:+.1f}", f"{o['bar']:.1f}", f"{o['edge']:+.1f}",
                                  "", ""), tags))
                    self.week_rows.append(None if o.get("player_id") is None
                                          else ("option", label, index, o["player_id"]))
        empty = ("Streaming decides a day at a time (strategy mode 'daily')." if p.get("stream_mode") != "week"
                 else f"No rentals worth a move {'next' if which == 'next' else 'this'} week.")
        self.week.set_rows(rows or [(("", "", "", "", "", "", empty) + ("",) * 11, ("empty",))])

    def _plan_a(self):
        """Plan A of the week shown (this week's, or next week's on its last day): the model's
        best week, and the one carrying the workbench your week is built on."""
        p = self.plan
        plans = p.get("next_week_plans") if self.week_which.get() == "next" else p.get("week_plans")
        return (plans or [None])[0]

    def _plans_shown(self) -> list:
        p = self.plan
        return (p.get("next_week_plans") if self.week_which.get() == "next" else p.get("week_plans")) or []

    def _build_book(self):
        """Your week shown (weekbook.Week) on plan A's workbench, or None in a plan saved before
        it carried one."""
        a = self._plan_a()
        bench = (a or {}).get("workbench")
        self.book = None if bench is None else weekbook.Week(bench, self.slot_order, self.accepts)

    def _my_moves(self) -> list:
        """Your picks in the week shown, put in (weekbook.Week.place) -- [] without a workbench."""
        if self.book is None:
            return []
        days = set(self.book.move_days)
        return self.book.place([(d, m["add"], m["drop"]) for d, c in self.choices.get("days", {}).items()
                                if d in days for m in c["moves"]])

    def _recommended(self, day) -> dict:
        """{player id: (the plans that pick him on `day` -- "A", or "B alt" for a slot's other
        option --, his best edge, the plan's drop)}: the slot picker lists these first."""
        out = {}
        for plan in self._plans_shown():
            for m in plan.get("moves", []):
                if m["day"] != day:
                    continue
                for o in m.get("options") or [{"player_id": m.get("add_id"), "edge": m["edge"]}]:
                    q = o.get("player_id")
                    if q is None:
                        continue
                    tag = plan["label"] + ("" if q == m.get("add_id") else " alt")
                    labels, edge, drop = out.get(q, ([], o["edge"], m.get("drop_id")))
                    out[q] = (labels + [tag], max(edge, o["edge"]), drop)
        return {q: (", ".join(labels), edge, drop) for q, (labels, edge, drop) in out.items()}

    def _fill_calendar(self):
        """The Weekly planner tab's calendar: your week day by day, built here from your picks (weekbook) --
        it starts empty, your roster. A column per day left this week: its lineup points and open
        slots, your picks that day (a cross takes one out; one that cannot go in says why, in red), then the league's lineup slots with who starts in each that night -- a rental in
        every slot he fills while held. Click an open slot or a rental's to pick who goes in it
        (`_open_slot_picker`). A pick is saved on the server at once and shows here at once."""
        for child in self.calendar.winfo_children():
            child.destroy()
        book = self.book
        if book is None:
            a = self._plan_a()
            self.week_note.set("")
            self.week_clear.state(["disabled"])
            message = ("Refresh to see the calendar (this plan was saved before the window built it)."
                       if a is not None else "No nights left to plan this week.")
            tk.Label(self.calendar, text=message, background="white", foreground="#6b7280").grid(row=0, column=0, padx=8, pady=8)
            return
        moves = self._my_moves()
        nights = book.nights_view(moves)
        left_out = [m for m in moves if m["problem"]]
        budget = book.bench["moves_left"]
        self.week_note.set(
            (f"Your picks: {len(moves)} · moves: {book.used(moves)} used, {budget - book.used(moves)} left this week"
             if moves else f"No picks yet · {budget} move(s) left this week")
            + (f" · {len(left_out)} left out" if left_out else ""))
        self.week_clear.state(["!disabled"] if moves else ["disabled"])
        if not nights:
            tk.Label(self.calendar, text="No nights left to plan this week.", background="white",
                     foreground="#6b7280").grid(row=0, column=0, padx=8, pady=8)
            return
        rentals = {m["add"] for m in book.made(moves)}
        bold, small = ("Segoe UI", 9, "bold"), ("Segoe UI", 8)
        cell = dict(width=CALENDAR_CELL_CHARS, anchor="w", justify="left", font=small,
                    wraplength=CALENDAR_CELL_CHARS * 7, padx=4, pady=2)
        tk.Label(self.calendar, text="Slot", font=bold, background="white").grid(row=0, column=0, sticky="w", padx=4)
        for column, n in enumerate(nights, 1):
            day = pd.Timestamp(n["day"])
            tk.Label(self.calendar, text=f"{day:%a %b} {day.day}\n{n['points']:.1f} pts · {len(n['open'])} open",
                     font=bold, background="white").grid(row=0, column=column, sticky="ew", padx=1)
            box = tk.Frame(self.calendar, background="white")
            box.grid(row=1, column=column, sticky="nsew", padx=1, pady=(0, 4))
            for m in n["moves"]:
                line = tk.Frame(box, background="white")
                line.pack(fill="x")
                text = (f"\U0001f4cc {book.name(m['add'])}"
                        + (f" ← {book.name(m['drop'])}" if m["drop"] is not None else " (open spot)"))
                if m["kind"] == "claim" and m["problem"] is None:
                    text += f" -- claim, from {pd.Timestamp(m['effective']):%a}"
                if m["problem"]:
                    text += f" -- left out: {m['problem']}"
                tk.Label(line, text=text, font=small, background="white", anchor="w", justify="left",
                         foreground="#b91c1c" if m["problem"] else "black",
                         wraplength=CALENDAR_CELL_CHARS * 6).pack(side="left", fill="x", expand=True)
                ttk.Button(line, text="✕", width=2,
                           command=lambda d=m["picked"], q=m["add"]: self._unpin(d, q)).pack(side="right")
        for row, slot in enumerate(self.slot_rows, 2):
            tk.Label(self.calendar, text=slot, font=bold, background="white").grid(row=row, column=0, sticky="w", padx=4)
        for column, n in enumerate(nights, 1):
            waiting = {}
            for s in n["started"]:
                waiting.setdefault(s["slot"], []).append(s)
            for row, slot in enumerate(self.slot_rows, 2):
                s = waiting.get(slot, []).pop(0) if waiting.get(slot) else None
                if s is None:
                    text, colour = "(open) ➕", CALENDAR_COLOURS["open"]
                else:
                    text = f"{s['player']}\n{s['pts']:.1f}" + ("  ⇄" if s["player_id"] in rentals else "")
                    colour = CALENDAR_COLOURS["pinned" if s["player_id"] in rentals else "held"]
                pickable = s is None or s["player_id"] in rentals
                label = tk.Label(self.calendar, text=text, background=colour,
                                 cursor="hand2" if pickable else "", **cell)
                label.grid(row=row, column=column, sticky="nsew", padx=1, pady=1)
                if pickable:
                    label.bind("<Button-1>", lambda event, d=n["day"], sl=slot, who=s:
                               self._open_slot_picker(d, sl, who))

    def _open_slot_picker(self, day, slot, occupant):
        """The slot picker: the free agents who fit in `day`'s lineup -- he would start that night,
        in any slot the lineup solve puts him (weekbook.free_agents_on; for a rental's slot, with
        the rental gone) -- filtered by position with the buttons above them -- the plans' picks and options that day first (best edge first, which plans in the
        Plans column), then the rest, most points that night first -- each with the week's lineup
        points he adds. Beside them,
        who to drop for the one selected: anyone you hold then, the move's week gain with each, the
        plan's drop chosen for a plan's player, and an open spot while the roster has room. For a
        rental's slot (`occupant`), the drop is that rental. Add saves the pick."""
        book = self.book
        if book is None:
            return
        moves = self._my_moves()
        found = book.free_agents_on(moves, day, occupant["player_id"] if occupant else None)
        recommended = self._recommended(day)
        found.sort(key=lambda f: (f["player_id"] not in recommended,
                                  -recommended[f["player_id"]][1] if f["player_id"] in recommended else -f["pts"],
                                  f["player"]))
        when = pd.Timestamp(day)
        top = tk.Toplevel(self.root)
        top.title(f"Add on {when:%a %b} {when.day}")
        top.transient(self.root)
        top.geometry("1100x560")
        counts = book.effective_on(day)
        ttk.Label(top, padding=8, wraplength=1060, justify="left",
                  text=(f"{when:%a %b} {when.day}, {slot} slot"
                        + (f": replacing {occupant['player']} from this night" if occupant else ": open")
                        + (f" (adds count from {pd.Timestamp(counts):%a})" if counts != day else "")
                        + ". Free agents who would start that night: the plans' picks first"
                          " (blue), then the most points that night. Week gain: the lineup points he"
                          " adds this week.")).pack(anchor="w")
        # Position filter: All, or a position some of them play (a player shows under each of his).
        positions = [q for q in POSITION_ORDER if any(q in f["positions"].split("/") for f in found)]
        shown = tk.StringVar(value="All")
        filters = ttk.Frame(top, padding=(8, 0, 8, 6))
        filters.pack(fill="x")
        ttk.Label(filters, text="Position:").pack(side="left", padx=(0, 6))
        for value in ["All", *positions]:
            ttk.Radiobutton(filters, text=value, value=value, variable=shown,
                            command=lambda: fill_adds()).pack(side="left", padx=4)
        panes = ttk.PanedWindow(top, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=8)

        def tree_in(columns, height=None):
            frame = ttk.Frame(panes)
            tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", selectmode="browse")
            for key, heading, width in columns:
                tree.heading(key, text=heading)
                tree.column(key, width=width, anchor="w" if key in ("player", "plans") else "center")
            scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
            tree.configure(yscrollcommand=scroll.set)
            scroll.pack(side="right", fill="y")
            tree.pack(fill="both", expand=True)
            tree.tag_configure("plan", background=PLAN_COLOUR)
            return frame, tree

        left, adds = tree_in((("plans", "Plans", 70), ("player", "Player", 200), ("positions", "Pos", 65),
                              ("pts", "Pts that night", 90), ("gain", "Week gain", 75),
                              ("games", "Games left", 75), ("status", "Status", 55)))
        right, drops = tree_in((("player", "Drop", 210), ("gain", "Week gain", 80)))
        panes.add(left, weight=3)
        panes.add(right, weight=2)
        empty = ttk.Label(top, padding=(8, 4))          # says when the filter leaves nobody

        def fill_adds():
            adds.delete(*adds.get_children())
            drops.delete(*drops.get_children())
            want = shown.get()
            listed = [f for f in found if want == "All" or want in f["positions"].split("/")]
            for f in listed:
                plans = recommended.get(f["player_id"], ("",))[0]
                adds.insert("", "end", iid=str(f["player_id"]), tags=("plan",) if plans else (),
                            values=(plans, f["player"], f["positions"], f"{f['pts']:.2f}", f"{f['gain']:+.1f}",
                                    f["games"], f.get("status") if f.get("status") not in (None, "ACTIVE") else ""))
            empty.configure(text="" if listed else
                            "No free agent who plays that night would start.")
            if listed:
                empty.pack_forget()
            else:
                empty.pack(before=panes, anchor="w")

        fill_adds()

        def show_drops(event=None):
            drops.delete(*drops.get_children())
            chosen = adds.selection()
            if not chosen:
                return
            add = int(chosen[0])
            if occupant is not None:
                his = next((m for m in book.made(moves) if m["add"] == occupant["player_id"]), None)
                # Picked the day he was added, the new player takes his move; later, a move of its own.
                gain = (book.gain([m for m in moves if m is not his], day, add, his["drop"])
                        if his is not None and his["picked"] == day
                        else book.gain(moves, day, add, occupant["player_id"]))
                if gain is None:
                    drops.insert("", "end", iid="none", values=("(no drop fits: no moves left -- take a pick out)", ""))
                    return
                drops.insert("", "end", iid=str(occupant["player_id"]), values=(occupant["player"], f"{gain:+.1f}"))
                drops.selection_set(str(occupant["player_id"]))
                return
            options = book.drops_for(moves, day, add)
            if not options:
                drops.insert("", "end", iid="none", values=("(no drop fits: no moves left -- take a pick out)", ""))
            for o in options:
                drops.insert("", "end", iid="open" if o["player_id"] is None else str(o["player_id"]),
                             values=(o["player"], f"{o['gain']:+.1f}"))
            plan_drop = recommended.get(add, (None, None, None))[2]
            preferred = next((str(o["player_id"]) for o in options if o["player_id"] == plan_drop), None)
            if options:
                drops.selection_set(preferred or ("open" if options[0]["player_id"] is None
                                                  else str(options[0]["player_id"])))

        def choose(event=None):
            chosen, dropped = adds.selection(), drops.selection()
            if not chosen or not dropped or dropped[0] == "none":
                return
            top.destroy()
            add = int(chosen[0])
            if occupant is not None:
                self._replace_rental(day, occupant["player_id"], add)
            else:
                self._pin(day, add, None if dropped[0] == "open" else int(dropped[0]))

        adds.bind("<<TreeviewSelect>>", show_drops)
        adds.bind("<Double-1>", choose)
        drops.bind("<Double-1>", choose)
        buttons = ttk.Frame(top, padding=8)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Cancel", command=top.destroy).pack(side="right", padx=4)
        ttk.Button(buttons, text="Add", command=choose).pack(side="right", padx=4)
        top.grab_set()
        self.picker = top                  # the window open now (a test can reach it)

    def _replace_rental(self, day, rental, player_id):
        """`player_id` in place of your rental `rental` from `day` on: picked the same day he was
        added, the new player takes his move (and his drop); later, he drops the rental."""
        new = copy.deepcopy(self.choices)
        chosen = self._day_choice(new, day)
        his = next((m for m in chosen["moves"] if m["add"] == rental), None)
        if his is not None:
            his["add"] = player_id
        else:
            chosen["moves"].append({"add": player_id, "drop": rental})
        self._save_choices(new)

    def _day_choice(self, choices, day) -> dict:
        return choices.setdefault("days", {}).setdefault(day, {"moves": []})

    def _pin(self, day, player_id, drop=None, choices=None):
        """Pick a free agent on `day`, dropping `drop` -- None: into an open spot. Saved, and shown
        at once. `choices`: add it to these (unsaved) instead."""
        new = choices if choices is not None else copy.deepcopy(self.choices)
        moves = self._day_choice(new, day)["moves"]
        if all(m["add"] != player_id for m in moves):
            moves.append({"add": player_id, "drop": drop})
        if choices is None:
            self._save_choices(new)

    def _unpin(self, day, player_id):
        new = copy.deepcopy(self.choices)
        chosen = self._day_choice(new, day)
        chosen["moves"] = [m for m in chosen["moves"] if m["add"] != player_id]
        self._save_choices(new)

    def _pick_from_plan(self, label, index, player_id):
        """A click on a plan's slot option: pick him on the slot's day with the plan's drop -- or,
        picked already, take it back. A drop the plan only picks up earlier (a rental chain) is
        that earlier slot's player you picked, or the plan's own pick there, picked with it."""
        plan = next((plan for plan in self._plans_shown() if plan["label"] == label), None)
        if plan is None:
            return
        move = plan["moves"][index]
        day = move["day"]
        if any(m["add"] == player_id for m in self.choices.get("days", {}).get(day, {}).get("moves", [])):
            self._unpin(day, player_id)
            return
        new = copy.deepcopy(self.choices)
        self._pin(day, player_id, self._plan_drop(plan, index, new), choices=new)
        self._save_choices(new)

    def _plan_drop(self, plan, index, choices):
        """Who slot `index` of `plan` drops, for a pick in your week: its drop when you hold him
        then, else -- an earlier slot's rental -- whoever you picked in that slot, or (none picked)
        that slot's pick, picked into `choices` with its own drop."""
        drop = plan["moves"][index].get("drop_id")
        earlier = next((i for i, m in enumerate(plan["moves"][:index]) if m.get("add_id") == drop), None)
        if drop is None or earlier is None:
            return drop
        slot = plan["moves"][earlier]
        options = {o.get("player_id") for o in slot.get("options") or []} | {drop}
        mine = next((m["add"] for m in choices.get("days", {}).get(slot["day"], {}).get("moves", [])
                     if m["add"] in options), None)
        if mine is not None:
            return mine
        self._pin(slot["day"], drop, self._plan_drop(plan, earlier, choices), choices=choices)
        return drop

    def _clear_week(self):
        """Forget your picks in the week shown."""
        days = set(self.book.move_days) if self.book is not None else set()
        new = copy.deepcopy(self.choices)
        new["days"] = {d: c for d, c in new.get("days", {}).items() if d not in days}
        self._save_choices(new)

    def _save_choices(self, new, replan=False):
        """Show the change at once and save it on the server behind; the server's answer is what
        was saved. The week needs no plan; with `replan` (the upgrades' drop list closed) the
        server plans again once the save is in."""
        self.choices = new
        if self.plan:
            self._fill_upgrade_menu()
            self._fill_week()
        def work():
            try:
                self.messages.put(("choices", self.server.set_choices(new)["choices"]))
                if replan:
                    self.messages.put(("start", "plan"))
            except Exception as error:  # noqa: BLE001 -- show it, keep the window
                self.messages.put(("error", f"saving your choices: {type(error).__name__}: {error}"))
        threading.Thread(target=work, daemon=True).start()

    def _toggle_week_plan(self, index):
        """A click on a plan's row opens it (its slots beneath) or closes it; on a slot's row, its
        options; on an option, picks him (`_pick_from_plan`)."""
        if not self.plan or index >= len(self.week_rows) or self.week_rows[index] is None:
            return
        key = self.week_rows[index]
        if key[0] == "option":
            self._pick_from_plan(*key[1:])
            return
        self.week_open[self.week_which.get()].symmetric_difference_update({key})
        self._fill_week()

    def _fill_players(self, table, players, where=False, **sort):
        rows = []
        for r in players:
            place = ("IR" if r["on_ir"] else "lineup" if r["in_lineup"] else "bench") if where else \
                    ("on waivers" if r["on_waivers"] else "")
            # Only injury colours: what the plan does with a player is in AI Suggestions.
            tags = (r["status"],) if r["status"] in STATUS_COLOURS else ()
            cells = {"player": r["player"], "positions": r["positions"], "status": r["status"] or "",
                     "rate": _num(r["rate"]), "ros_points": _num(r.get("ros_points"), 1),
                     "peripheral": _pct(r.get("peripheral")),
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
            "rate", "ros_points", "peripheral", "per_game", "plays_tonight", "games_left",
            *self.stat_keys)
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
        status = self.served.get("status")
        if not status:
            return
        lines = [f"{step}: {clock(dt.datetime.fromisoformat(when))}" for step, when in status["last"].items()]
        if status.get("skip_snapshots"):
            lines.append("reports: from the scheduled snapshots (--skip-snapshots)")
        if status.get("league_file"):
            lines.append(f"league: {status['league_file']} (not the platform)")
        lines.append(f"server: {self.server.url}, day {status['day']}")
        self.fresh_var.set("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--league", default=leagues.DEFAULT_LEAGUE, help="A league in Settings/leagues/")
    parser.add_argument("--server", default=DEFAULT_SERVER,
                        help=f"The plan server's address (default {DEFAULT_SERVER}; Server/server.py)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    root = tk.Tk()
    PlanWindow(root, args)
    root.mainloop()


if __name__ == "__main__":
    main()

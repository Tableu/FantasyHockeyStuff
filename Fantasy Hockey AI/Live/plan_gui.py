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
    Moves        IR moves, adds and drops, claims -- with the rate each was priced on
    Upgrade      permanent pickups: the plan's upgrades and claims, highlighted, above the add/drop
                 rule's own pricing of the top free agents on the roster you hold now, each with his
                 best drop, the lineup points he gains over the pricing window, the bar a move must
                 clear and the edge (gain - bar) they are ranked by (rentals: the Week tab)
    Week         the week's streaming plans by NHL team (strategy mode 'week', Decisions/weekplan.py
                 TeamPlans): each rental is a team slot -- day, team, position -- that any of the
                 players on that team who fit it can fill. One row per plan: its schedule
                 ("Tue NYR RW -> Thu CGY C ..."), what it adds this week, its expected edge (each
                 slot's options discounted by the chance each is taken first) and its thinnest slot.
                 Plan A is the one made (today's slots are the Moves); B, C, ... each leave out every
                 earlier plan's first team -- the fallbacks when a team is picked over. Click a
                 plan for its slots, a slot for its options, ranked by edge; the later slots are
                 planned again on every run. On the week's last day (Sunday) a switch shows next
                 week's plans instead: planned from its first day on the roster today's moves leave,
                 with a fresh move limit -- a preview, planned again once that week starts
    Roster       every player you hold now: status, rate, rest-of-season points, Periph % (the share
                 of his projected points from hits, blocks, shots and PIM: high = steady, low = a
                 volatile scorer), games left this week and his stats (sortable); injured players
                 coloured by status. Click a player to mark him OK to drop (click again to unmark):
                 while any are marked, the plan's upgrades and rentals drop only them -- or a rental
                 the week plan picks up itself -- goalies and starters included, even when that
                 leaves a lineup slot empty (Wolf for a skater: roster.fill_check = none);
                 none marked, the model chooses. Saved on the server for the league
                 (Live/droppable.py); the next
                 refresh plans on it. Forced drops (an IR activation into a full roster) stay the
                 model's
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
import leagues
import sheets
import simlayer

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
ROW_STYLES = {"plan": {"bg": PLAN_COLOUR}, "empty": {"fg": "#9ca3af"},
              **{status: {"bg": colour} for status, colour in STATUS_COLOURS.items()},
              **{owner: {"bg": colour} for owner, colour in OWNER_COLOURS.items()}}


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
        return self._call("/refresh", {"mode": mode})

    def job(self, job_id, after=0):
        return self._call(f"/jobs/{job_id}?after={after}")

    def droppable(self):
        """The ids of the players marked OK to drop ([]: the model chooses)."""
        return self._call("/droppable")["player_ids"]

    def set_droppable(self, player_ids):
        return self._call("/droppable", {"player_ids": sorted(player_ids)}, method="PUT")["player_ids"]

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
        # The players marked OK to drop (the Roster tab; the server keeps the list), and the
        # Roster table's rows' player ids, in their shown order, for a click.
        self.droppable, self.roster_ids = set(), []

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
        scored = set(scoring.skaters) | set(scoring.goalies)
        self.stat_keys = [k for k in draft_board.STAT_HEADINGS if k == "gp" or k in scored]
        stat_columns = [(k, draft_board.STAT_HEADINGS[k], 48) for k in self.stat_keys]
        self.tonight = self._table("Tonight", [("slot", "Slot", 60), ("player", "Player", 240),
                                               ("mean", "Exp. pts", 80), ("sd", "SD", 60),
                                               ("p", "P(plays/starts)", 110), ("puck", "Puck", 90),
                                               ("flag", "Flag", 70), ("lock", "", 40)] + stat_columns)
        self.moves = self._table("Moves", [("kind", "Move", 110), ("add", "Add", 260), ("add_rate", "pts/g", 70),
                                           ("drop", "Drop", 260), ("drop_rate", "pts/g", 70), ("note", "Note", 200)])
        self.options = self._table("Upgrade", [("rank", "#", 36), ("kind", "Move", 90), ("add", "Add", 230),
                                               ("add_rate", "pts/g", 60), ("add_periph", "Periph", 60),
                                               ("add_games", "Games", 60),
                                               ("drop", "Drop", 230), ("drop_rate", "pts/g", 60),
                                               ("drop_games", "Games", 60), ("gain", "Gain", 70),
                                               ("bar", "Bar", 60), ("edge", "Edge", 60), ("note", "", 150)])
        # Plans and slots opened on the Week tab, this week's and next week's apart: ("plan", label)
        # and ("slot", label, index). Plan A starts open.
        self.week_open = {"this": {("plan", "A")}, "next": {("plan", "A")}}
        self.week_rows = []                # Week tab row -> its plan's or slot's key (None: an option)
        self.week = sheets.Table(self._build_week_bar(), [("plan", "Plan", 70), ("day", "Day", 110),
                                         ("until", "Dropped", 70), ("team", "Team", 50),
                                         ("pos", "Pos", 60), ("kind", "Move", 90), ("add", "Add", 300),
                                         ("add_rate", "pts/g", 60), ("add_periph", "Periph", 60),
                                         ("add_games", "Games", 60),
                                         ("drop", "Drop", 230), ("drop_rate", "pts/g", 60),
                                         ("drop_games", "Games", 60), ("gain", "Gain", 70),
                                         ("bar", "Bar", 60), ("edge", "Edge", 60),
                                         ("expected", "Exp.", 60), ("depth", "Options", 65)],
                                 ROW_STYLES, on_row_click=self._toggle_week_plan)
        player_columns = [("player", "Player", 240), ("positions", "Pos", 80), ("status", "Status", 70),
                          ("rate", "Rate (pts/g)", 90), ("ros_points", "ROS pts", 70),
                          ("peripheral", "Periph %", 70),
                          ("per_game", "Tonight's proj.", 100),
                          ("plays_tonight", "Plays tonight", 95), ("games_left", "Games left", 80),
                          ("where", "", 90), ("plan", "Recommended", 110)]
        # The roster you hold: no tonight columns, no lineup/bench/IR column, no recommended action.
        self.roster = self._build_roster([("drop_ok", "OK to drop", 80)]
                                         + [c for c in player_columns + stat_columns
                                            if c[0] not in ROSTER_HIDDEN])
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

    def _build_roster(self, columns):
        """The Roster tab: who is marked OK to drop and a button to clear them, above the table; a
        click on a player marks or unmarks him."""
        frame = ttk.Frame(self.tabs)
        self.tabs.add(frame, text="Roster")
        bar = ttk.Frame(frame, padding=(0, 6, 0, 4))
        bar.pack(fill="x")
        self.droppable_var = tk.StringVar()
        ttk.Label(bar, textvariable=self.droppable_var).pack(side="left", padx=6)
        self.droppable_clear = ttk.Button(bar, text="Clear (let the model choose)",
                                          command=lambda: self._save_droppable(set()))
        self.droppable_clear.pack(side="left", padx=6)
        table = ttk.Frame(frame)
        table.pack(fill="both", expand=True)
        self._show_droppable()
        return sheets.Table(table, columns, ROW_STYLES, on_sort=lambda key: self._sort("roster", key),
                            on_row_click=self._toggle_droppable)

    def _show_droppable(self):
        marked = len(self.droppable)
        self.droppable_var.set(
            "Click a player to mark him OK to drop. None marked: the plan chooses its own drops."
            if not marked else
            f"{marked} marked OK to drop: the plan drops only from these (and rentals it picks up itself). "
            "Refresh to re-plan.")
        self.droppable_clear.state(["!disabled"] if marked else ["disabled"])

    def _toggle_droppable(self, index):
        if index >= len(self.roster_ids):
            return
        self._save_droppable(self.droppable ^ {self.roster_ids[index]})

    def _save_droppable(self, marked):
        """Show the change at once, save it on the server behind; the server's answer is the list."""
        self.droppable = set(marked)
        self._show_droppable()
        self._fill_sorted("roster")
        def work():
            try:
                self.messages.put(("droppable", self.server.set_droppable(marked)))
            except Exception as error:  # noqa: BLE001 -- show it, keep the window
                self.messages.put(("error", f"saving who is OK to drop: {type(error).__name__}: {error}"))
        threading.Thread(target=work, daemon=True).start()

    def _build_week_bar(self):
        """The Week tab: a this week / next week switch, shown only when the plan has next week's
        plans (the week's last day), above the plans table. Returns the table's frame."""
        frame = ttk.Frame(self.tabs)
        self.tabs.add(frame, text="Week")
        self.week_bar = bar = ttk.Frame(frame, padding=(0, 6, 0, 4))
        self.week_which = tk.StringVar(value="this")
        self.week_choices = []
        for value in ("this", "next"):
            button = ttk.Radiobutton(bar, value=value, variable=self.week_which, command=self._fill_week)
            button.pack(side="left", padx=6)
            self.week_choices.append(button)
        self.week_table_frame = ttk.Frame(frame)
        self.week_table_frame.pack(fill="both", expand=True)
        return self.week_table_frame

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
        """Start a refresh on the server, followed on a worker thread: 'full' or 'quick'."""
        if self.busy:
            return
        self.busy = True
        self.full_button.state(["disabled"])
        self.quick_button.state(["disabled"])
        self.status_var.set("Full refresh running..." if mode == "full" else "Quick refresh running...")
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
                    self.status_var.set(f"Server refresh running ({payload['by']})...")
                    threading.Thread(target=self._follow, args=(payload["id"],), daemon=True).start()
            elif kind == "droppable":             # the server's list of who is OK to drop
                self.droppable = set(payload)
                self._show_droppable()
                self._fill_sorted("roster")
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
                self.messages.put(("droppable", self.server.droppable()))
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
        """A server refresh's progress lines until it ends, then its plan (on a worker thread)."""
        try:
            after = 0
            while True:
                job = self.server.job(job_id, after)
                for line in job["lines"]:
                    self.messages.put(("log", line))
                after = job["next"]
                if job["state"] != "running":
                    break
                time.sleep(JOB_POLL_S)
            self.messages.put(("status", self.server.status()))
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
                          + (f" · {len(p['week_plan'])} rental(s) planned this week"
                             if p.get("stream_mode") == "week" else "")
                          + ("" if p.get("games_today", True) else " · no NHL games today")
                          # The drop list overrides the model's drops until cleared: always visible.
                          + (f" · drops limited to {len(p['droppable'])} marked player(s)"
                             if p.get("droppable") else ""))

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

        rows = []
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
        """The Week tab, three levels deep. A plan's row: its schedule (one team slot per move), what
        it adds this week and its expected edge, the teams it leaves out. Under a plan clicked open,
        its slots: day, team, position, the best option and its drop. Under a slot clicked open, the
        players on that team who fit it, ranked by edge -- any of them buys the same nights. Plan A
        is the one made; only its moves today are the Moves, and they are highlighted."""
        p = self.plan
        # Next week's plans exist on the week's last day only; any other day the switch is hidden
        # and this week shows.
        has_next = bool(p.get("next_week_plans"))
        if has_next:
            self.week_choices[0].configure(text=f"This week (week {p['week']})")
            self.week_choices[1].configure(text=f"Next week (week {p['next_week']})")
            self.week_bar.pack(fill="x", before=self.week_table_frame)
        else:
            self.week_bar.pack_forget()
            self.week_which.set("this")
        which = self.week_which.get()
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
                made = which == "this" and label == "A" and w["today"]
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
                for rank, o in enumerate(options, 1):
                    start = "" if o["from"] == w["from"] else f" (from {pd.Timestamp(o['from']).strftime('%a')})"
                    status = f" [{o['status']}]" if o.get("status") not in (None, "ACTIVE") else ""
                    rows.append((("", "", "", "", o["positions"], o["kind"], f"      {rank}. {o['add']}{status}{start}",
                                  _num(o["add_rate"]), _pct(o.get("add_periph")), o["add_games"],
                                  "", "", "", f"{o['gain']:+.1f}", f"{o['bar']:.1f}", f"{o['edge']:+.1f}",
                                  "", ""),
                                 (o["status"],) if o.get("status") in STATUS_COLOURS else ()))
                    self.week_rows.append(None)
        empty = ("Streaming decides a day at a time (strategy mode 'daily')." if p.get("stream_mode") != "week"
                 else f"No rentals worth a move {'next' if which == 'next' else 'this'} week.")
        self.week.set_rows(rows or [(("", "", "", "", "", "", empty) + ("",) * 11, ("empty",))])

    def _toggle_week_plan(self, index):
        """A click on a plan's row opens it (its slots beneath) or closes it; on a slot's row, its
        options."""
        if not self.plan or index >= len(self.week_rows) or self.week_rows[index] is None:
            return
        self.week_open[self.week_which.get()].symmetric_difference_update({self.week_rows[index]})
        self._fill_week()

    def _fill_players(self, table, players, where=False, **sort):
        rows = []
        for r in players:
            place = ("IR" if r["on_ir"] else "lineup" if r["in_lineup"] else "bench") if where else \
                    ("on waivers" if r["on_waivers"] else "")
            # Only injury colours: what the plan does with a player is on the Moves tab.
            tags = (r["status"],) if r["status"] in STATUS_COLOURS else ()
            cells = {"drop_ok": "\u2713" if r.get("player_id") in self.droppable else "",
                     "player": r["player"], "positions": r["positions"], "status": r["status"] or "",
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
            text = key in ("player", "positions", "status", "plan", "where", "drop_ok")
            value = ((lambda r: (r.get("stats") or {}).get(key)) if key in self.stat_keys
                     else (lambda r: "\u2713" if r.get("player_id") in self.droppable else " ")
                     if key == "drop_ok"
                     else (lambda r: r[key]) if key != "where" else (lambda r: r["player"]))
            # Blanks last either way: a goalie has no hits, a player with no games no line.
            present = [r for r in players if value(r) is not None]
            players = (sorted(present, key=(lambda r: value(r) or "") if text else value,
                              reverse=descending)
                       + [r for r in players if value(r) is None])
        if name == "roster":
            self.roster_ids = [r.get("player_id") for r in players]
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

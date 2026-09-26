#!/usr/bin/env python
"""Draft-night window: the live draft board and the rankings, in a spreadsheet-like view.

    python draft_gui.py                          # follow the live draft (league beagles)
    python draft_gui.py --manual                 # the API is down: double-click to pick
    python draft_gui.py --league espn --slot 10  # a league it cannot read (ESPN): double-click picks

The same board and matching as `draft_assistant.py` (read only; it never submits a pick), in a
tkinter window instead of the terminal:

- **Rankings**: every player on the board, by value over replacement, with his projected line in
  every stat the league scores; the Pos cell is coloured by position, the Tier cell by tier and
  OFF/POG on a red-white-blue scale (the aggregate workbook's shading). Click a column header to sort by it; filter by position, by
  "fills one of my open slots", or by name; taken players are hidden (greyed when "show taken" is
  ticked). Reset view puts the sort, filter, search and "show taken" back.
- **Draft board**: rounds down, teams across in draft order, each pick coloured by position, the
  pick on the clock highlighted and your column marked.
- **⚙ Settings**: a popup laid out like the aggregate workbook's Settings sheet -- whose positions
  and ADP, the playoff dates, the roster (teams, slots, bench) and the scoring. Apply rebuilds the
  board; Save as default writes the league's Settings/leagues/ file (see `open_settings`).
- **Sidebar**: the clock, your next picks, open starting slots, the positional-need warning, your
  roster, the last picks and any pick it could not match.

With `--manual`, double-click a player to record the pick on the clock; Undo takes the last back.
"""

import argparse
import datetime as dt
import logging
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk

import pandas as pd
from tksheet import Sheet

import seasonlayer  # noqa: F401 -- puts Season/ on sys.path; see seasonlayer.py
import draft_assistant as da
from decisionlayer import draft as draft_module

POSITION_COLOURS = {"C": "#cfe2ff", "LW": "#d1f0d8", "RW": "#fde2c4", "D": "#e6d8f5",
                    "G": "#f8d0d6"}
# The aggregate workbook's TIER shading (TIER_SHADING_PALETTE), by tier number, cycling after 10.
TIER_COLOURS = ("#d9f0d4", "#d1e0f7", "#fcf0c4", "#fcd9ba", "#f7cccc", "#e3d4f2", "#ccede8",
                "#e0e0e0", "#e6d9bf", "#f2cce6")
TAKEN_BG, TAKEN_FG = "#f3f4f6", "#9ca3af"
TABLE_FONT = ("Segoe UI", 9, "normal")
# The aggregate workbook's OFF and POG colour scale (Player Values): the column's minimum light
# red, its median white, its maximum blue, blended in between.
SCALE_LOW, SCALE_MID, SCALE_HIGH = "#ea9999", "#ffffff", "#4285f4"
CLOCK_COLOUR = "#fff3a0"
MINE_COLOUR = "#1d4ed8"
FILTERS = ("All", "C", "LW", "RW", "F (C/LW/RW)", "D", "G", "Fills my slot")

COLUMNS = [  # (key, heading, width, anchor)
    ("rank", "#", 44, "e"), ("injury", "\U0001fa79", 50, "center"), ("player", "Player", 170, "w"),
    ("team", "Team", 50, "center"),
    ("positions", "Pos", 64, "center"), ("value", "Value", 60, "e"), ("vor", "VOR", 56, "e"),
    ("tier", "Tier", 80, "center"), ("periph_pct", "Periph %", 66, "e"),
    ("sources", "Src", 40, "e"),
    ("adp", "ADP", 136, "e"), ("gone", "By next pick", 90, "center"),
]
# The aggregate workbook's schedule columns (draft_board.schedule_counts), when the playoff weeks
# are known: games on nights with 8 or fewer NHL games through the fantasy playoffs, and games
# during the fantasy playoffs.
SCHEDULE_HEADINGS = {"off": "OFF", "pog": "POG"}
# Every stat the league scores (draft_board.stat_lines: the line the value came from), + GP.
STAT_HEADINGS = {"gp": "GP", "goals": "G", "assists": "A", "ppp": "PPP", "shp": "SHP",
                 "hits": "HIT", "blocks": "BLK", "shots": "SOG", "pim": "PIM", "wins": "W",
                 "losses": "L", "ot_losses": "OTL", "shutouts": "SO", "saves": "SV",
                 "goals_against": "GA"}
# The settings popup's scoring fields: every quantity the projections carry (Simulation/scoring.py
# SIDES), labelled as the aggregate workbook labels them.
SKATER_SCORING = (("goals", "G"), ("assists", "A"), ("ppp", "PPP"), ("shp", "SHP"),
                  ("shots", "SOG"), ("hits", "HIT"), ("blocks", "BLK"), ("pim", "PIM"))
GOALIE_SCORING = (("wins", "W"), ("losses", "L"), ("goals_against", "GA"), ("saves", "SV"),
                  ("shutouts", "SO"), ("ot_losses", "OTL"))
ADP_NAMES = {"espn": "ESPN", "yahoo": "Yahoo", "fantrax": "Fantrax", "fleaflicker": "Fleaflicker",
             "oldtimehockey": "Old Time Hockey"}
# Dobber's Band-Aid Boys (draft_board: `injury`), as the aggregate workbook's 🩹 column. Sorted
# Certified, Goalie, Trainee -- the goalies are listed apart, with no tier of their own.
INJURY_ORDER = {"Certified": 0, "Goalie": 1, "Trainee": 2}
INJURY_LETTER = {"Certified": "C", "Trainee": "T", "Goalie": "G"}      # shown as 🩹 C / T / G
DESCENDING = {"value", "vor", "periph_pct", "sources", "off", "pog", "gp", "goals", "assists",
              "ppp", "shp", "hits", "blocks", "shots", "pim", "wins", "ot_losses", "shutouts", "saves"}


def _num(x, digits=0):
    return "" if x is None or pd.isna(x) else f"{x:.{digits}f}"


def _scale_points(column):
    """A column's (min, median, max) over the whole board, as the workbook's scale reads it; None
    for an empty column."""
    values = column.dropna().astype(float)
    return None if values.empty else (values.min(), values.median(), values.max())


def _blend(a, b, t):
    rgb = [round(int(a[i:i + 2], 16) + (int(b[i:i + 2], 16) - int(a[i:i + 2], 16)) * t)
           for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(rgb)


def _scale_colour(v, low, mid, high):
    """SCALE_LOW at `low`, SCALE_MID at `mid`, SCALE_HIGH at `high`, linear in between."""
    if v <= mid:
        return _blend(SCALE_LOW, SCALE_MID, 1.0 if mid == low else (v - low) / (mid - low))
    return _blend(SCALE_MID, SCALE_HIGH, 1.0 if high == mid else (v - mid) / (high - mid))


class DraftWindow:
    def __init__(self, root, assistant, args, board_json, my_team, my_name):
        self.root, self.a, self.args = root, assistant, args
        self.board_json, self.my_team, self.my_name = board_json, my_team, my_name
        self.teams = board_json.get("draftOrder", [])
        self.manual_picks = []
        self.sort_key, self.sort_reverse = "rank", False
        self.cells = da.picks_from(board_json)
        self.updates = queue.Queue()

        root.title(f"Draft -- {args.league}: {my_name}")
        root.geometry("1500x860")
        try:
            root.state("zoomed")                  # 14 teams across need the full width
        except tk.TclError:
            pass
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
        self._build_rankings()
        self._build_board()
        body.add(self._build_sidebar(body), weight=1)

        self.refresh()
        if not args.manual:
            threading.Thread(target=self._poll, daemon=True).start()
            root.after(500, self._drain)

    # -- layout -------------------------------------------------------------------------------
    def _build_toolbar(self):
        bar = ttk.Frame(self.root, padding=(8, 8, 8, 4))
        bar.pack(fill="x")
        self.clock_var = tk.StringVar()
        ttk.Label(bar, textvariable=self.clock_var, style="Big.TLabel").pack(side="left")
        ttk.Button(bar, text="⚙", width=3, command=self.open_settings).pack(side="right",
                                                                            padx=(4, 0))
        ttk.Button(bar, text="Refresh", command=self.fetch_now).pack(side="right")
        ttk.Button(bar, text="Reset view", command=self.reset_view).pack(side="right", padx=4)
        if self.args.manual:
            ttk.Button(bar, text="Undo pick", command=self.undo).pack(side="right", padx=4)
        self.show_taken = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Show taken", variable=self.show_taken,
                        command=self.fill_rankings).pack(side="right", padx=8)
        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *_: self.fill_rankings())
        ttk.Entry(bar, textvariable=self.search, width=20).pack(side="right")
        ttk.Label(bar, text="Search").pack(side="right", padx=(8, 4))
        # Set in the settings popup (the gear), starting from Settings/strategy.json's draft block.
        self.adp_choice = tk.StringVar(value=self._adp_name(self.a.adp_platform))
        self.positions_choice = tk.StringVar(value=self._platform_name(self.a.eligibility_platform))
        self.settings_window = None
        self.filter = tk.StringVar(value=FILTERS[0])
        box = ttk.Combobox(bar, textvariable=self.filter, values=FILTERS, state="readonly", width=14)
        box.pack(side="right")
        box.bind("<<ComboboxSelected>>", lambda _: self.fill_rankings())
        ttk.Label(bar, text="Position").pack(side="right", padx=(8, 4))

    def open_settings(self):
        """The gear's popup, laid out like the aggregate workbook's Settings sheet:

        - Board: whose positions the board values players on ("Site Used (POS)") and whose ADP it
          shows -- both apply as soon as they are picked;
        - Playoffs Schedule: the fantasy playoffs' first and last day (OFF and POG);
        - Roster Settings: teams and the slots per team (UTIL(F/D) is this league's F/D);
        - Scoring: the points per stat, skaters and goalies (0 = not scored);
        - VORP Tier Settings: the Tier Gap Z-Score -- how big a drop in value starts a new tier.

        Apply rebuilds the board for this session; Save as default also writes it into the
        league's Settings/leagues/ file, which the draft tools start from next time. League defaults
        puts back the league and scoring files and the platform's playoff weeks. The simulator never
        reads any of it."""
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.lift()
            self.settings_window.focus_set()
            return
        win = self.settings_window = tk.Toplevel(self.root)
        win.title("Settings")
        win.transient(self.root)
        win.resizable(False, False)
        outer = ttk.Frame(win, padding=16)
        outer.pack(fill="both", expand=True)
        bold = ("Segoe UI", 10, "bold")
        muted = "#6b7280"

        board = ttk.LabelFrame(outer, text="Board", padding=10)
        board.grid(row=0, column=0, columnspan=2, sticky="ew")
        for i, (label, var, values, apply) in enumerate((
                ("Positions from", self.positions_choice,
                 [self._platform_name(p) for p in self.a.eligibility_platforms], self.set_positions),
                ("ADP", self.adp_choice, [self._adp_name(p) for p in self.a.adp_platforms],
                 self.set_adp))):
            ttk.Label(board, text=label, font=bold).grid(row=0, column=2 * i, sticky="w",
                                                         padx=(0 if i == 0 else 16, 6))
            box = ttk.Combobox(board, textvariable=var, values=values, state="readonly", width=12)
            box.grid(row=0, column=2 * i + 1, sticky="w")
            box.bind("<<ComboboxSelected>>", lambda _, apply=apply: apply())

        playoffs = ttk.LabelFrame(outer, text="Playoffs Schedule", padding=10)
        playoffs.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.playoff_vars = []
        for i, label in enumerate(("Start Date", "End Date")):
            ttk.Label(playoffs, text=label, font=bold).grid(row=0, column=2 * i, sticky="w",
                                                            padx=(0 if i == 0 else 16, 6))
            var = tk.StringVar()
            ttk.Entry(playoffs, textvariable=var, width=12).grid(row=0, column=2 * i + 1)
            self.playoff_vars.append(var)
        ttk.Label(playoffs, text="YYYY-MM-DD. OFF: games on nights with 8 or fewer NHL games "
                                 "through the end date. POG: games from start to end.",
                  foreground=muted, wraplength=460).grid(row=1, column=0, columnspan=4,
                                                         sticky="w", pady=(4, 0))

        roster = ttk.LabelFrame(outer, text="Roster Settings", padding=10)
        roster.grid(row=2, column=0, sticky="nsew", pady=(10, 0), padx=(0, 10))
        self.roster_vars = {}
        rows = [("TEAMS", "teams")] + [("UTIL(F/D)" if s == "F/D" else s, s)
                                       for s in da.ROSTER_SLOTS] + [("BN", "bench")]
        for r, (label, key) in enumerate(rows):
            ttk.Label(roster, text=label, font=bold if key == "teams" else None).grid(
                row=r, column=0, sticky="w", padx=(0, 10))
            var = self.roster_vars[key] = tk.StringVar()
            var.trace_add("write", lambda *_: self._update_roster_total())
            ttk.Entry(roster, textvariable=var, width=6, justify="right").grid(row=r, column=1,
                                                                                pady=1)
        self.roster_total = tk.StringVar()
        ttk.Label(roster, text="ROSTER", font=bold).grid(row=len(rows), column=0, sticky="w",
                                                         pady=(4, 0))
        ttk.Label(roster, textvariable=self.roster_total, font=bold).grid(
            row=len(rows), column=1, sticky="e", pady=(4, 0))

        scoring = ttk.LabelFrame(outer, text="Scoring (points per)", padding=10)
        scoring.grid(row=2, column=1, sticky="nsew", pady=(10, 0))
        self.scoring_vars = {}
        for c, (side, title, quantities) in enumerate((
                ("skaters", "SKATERS", SKATER_SCORING), ("goalies", "GOALIES", GOALIE_SCORING))):
            ttk.Label(scoring, text=title, font=bold).grid(row=0, column=2 * c, columnspan=2,
                                                           sticky="w", padx=(0 if c == 0 else 18, 0))
            for r, (key, label) in enumerate(quantities, 1):
                ttk.Label(scoring, text=label).grid(row=r, column=2 * c, sticky="w",
                                                    padx=(0 if c == 0 else 18, 8))
                var = self.scoring_vars[(side, key)] = tk.StringVar()
                ttk.Entry(scoring, textvariable=var, width=7, justify="right").grid(
                    row=r, column=2 * c + 1, pady=1)
        ttk.Label(scoring, text="0 = not scored. Only stats the projections carry are listed.",
                  foreground=muted, wraplength=260).grid(row=len(SKATER_SCORING) + 1, column=0,
                                                         columnspan=4, sticky="w", pady=(6, 0))

        tiers = ttk.LabelFrame(outer, text="VORP Tier Settings", padding=10)
        tiers.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Label(tiers, text="Tier Gap Z-Score", font=bold).grid(row=0, column=0, sticky="w",
                                                                  padx=(0, 6))
        self.tier_gap_z = tk.StringVar()
        ttk.Entry(tiers, textvariable=self.tier_gap_z, width=6, justify="right").grid(
            row=0, column=1, sticky="w")
        ttk.Label(tiers, text="A new tier starts where the drop in value to the next player at "
                              "the position is more than the average drop plus this many standard "
                              "deviations. Lower gives more tiers.",
                  foreground=muted, wraplength=460).grid(row=1, column=0, columnspan=2,
                                                         sticky="w", pady=(4, 0))

        self.settings_status = tk.StringVar()
        ttk.Label(outer, textvariable=self.settings_status, foreground=muted,
                  wraplength=460).grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))
        buttons = ttk.Frame(outer)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(buttons, text="League defaults", command=self.settings_defaults).pack(
            side="left", padx=(0, 6))
        ttk.Button(buttons, text="Apply", command=self.settings_apply).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="Save as default", command=self.settings_save).pack(
            side="left", padx=(0, 6))
        ttk.Button(buttons, text="Close", command=win.destroy).pack(side="left")
        win.bind("<Escape>", lambda _: win.destroy())

        self._fill_settings(self.a.playoffs, self.a.config, self.a.scoreset(),
                            self.a.tier_gap_z())
        win.update_idletasks()
        x = self.root.winfo_rootx() + self.root.winfo_width() - win.winfo_reqwidth() - 40
        y = self.root.winfo_rooty() + 50
        win.geometry(f"+{max(x, 0)}+{y}")

    def _fill_settings(self, playoffs, config, scoreset, tier_gap_z):
        for var, day in zip(self.playoff_vars, playoffs or ("", "")):
            var.set(pd.Timestamp(day).strftime("%Y-%m-%d") if day != "" else "")
        self.roster_vars["teams"].set(str(config.teams))
        self.roster_vars["bench"].set(str(config.bench))
        for slot in da.ROSTER_SLOTS:
            self.roster_vars[slot].set(str(config.active_slots.get(slot, 0)))
        for (side, key), var in self.scoring_vars.items():
            var.set(f"{scoreset.weights(side).get(key, 0.0):g}")
        self.tier_gap_z.set(f"{tier_gap_z:g}")
        self._update_roster_total()

    def _update_roster_total(self):
        try:
            total = sum(int(self.roster_vars[k].get()) for k in list(da.ROSTER_SLOTS) + ["bench"])
            self.roster_total.set(str(total))
        except ValueError:
            self.roster_total.set("?")

    def _read_settings(self) -> dict:
        """The popup's fields as the league's draft_window overrides; ValueError names a bad field."""
        days = [v.get().strip() for v in self.playoff_vars]
        if any(days) and not all(days):
            raise ValueError("give both playoff dates, or neither")
        try:
            playoffs = [pd.Timestamp(d).strftime("%Y-%m-%d") for d in days] if all(days) else None
        except ValueError:
            raise ValueError(f"playoff dates must be YYYY-MM-DD; got {days}") from None
        if playoffs and playoffs[0] > playoffs[1]:
            raise ValueError("the playoff start date is after the end date")

        def whole(key):
            text = self.roster_vars[key].get().strip()
            if not text.isdigit():
                raise ValueError(f"{key}: a whole number, not {text!r}")
            return int(text)

        def number(side, key):
            text = self.scoring_vars[(side, key)].get().strip() or "0"
            try:
                return float(text)
            except ValueError:
                raise ValueError(f"{side} {key}: a number, not {text!r}") from None

        text = self.tier_gap_z.get().strip()
        try:
            tier_gap_z = float(text)
        except ValueError:
            raise ValueError(f"Tier Gap Z-Score: a number, not {text!r}") from None
        return {
            "playoffs": playoffs,
            "roster": {"teams": whole("teams"), "bench": whole("bench"),
                       "slots": {s: whole(s) for s in da.ROSTER_SLOTS}},
            "scoring": {side: {key: number(side, key) for key, _ in quantities}
                        for side, quantities in (("skaters", SKATER_SCORING),
                                                 ("goalies", GOALIE_SCORING))},
            "tier_gap_z": tier_gap_z,
        }

    def settings_apply(self) -> bool:
        """Rebuild the board on the popup's playoff dates, roster and scoring. A setting the
        league config refuses (an odd team count, fewer teams than playoff spots) is reported and
        nothing changes."""
        try:
            fields = self._read_settings()
        except ValueError as error:
            self.settings_status.set(f"Not applied: {error}")
            return False
        before = (dict(self.a.overrides), self.a.playoffs)
        self.a.overrides.update(fields)
        self.a.playoffs = (tuple(pd.Timestamp(d) for d in fields["playoffs"])
                           if fields["playoffs"] else None)
        try:
            self.a.build_board(self.a.eligibility_platform)
        except (ValueError, SystemExit) as error:
            self.a.overrides, self.a.playoffs = before
            self.a.build_board(self.a.eligibility_platform)
            self.settings_status.set(f"Not applied: {error}")
            return False
        self._set_columns()
        self.refresh()
        levels = ", ".join(f"{s} {v:.1f}" for s, v in self.a.levels.items())
        self.settings_status.set(f"Applied. {self.a.config.teams} teams, "
                                 f"{self.a.config.roster_size}-man rosters; replacement {levels}.")
        return True

    def settings_save(self):
        if not self.settings_apply():
            return
        self.a.overrides["eligibility_platform"] = self.a.eligibility_platform
        self.a.overrides["adp_platform"] = self.a.adp_platform
        path = self.a.save_overrides()
        self.settings_status.set(self.settings_status.get() + f" Saved to {path.name}.")

    def settings_defaults(self):
        """The league and scoring files and the platform's playoff weeks, back in the fields and
        applied (not saved: Save as default does that)."""
        self._fill_settings(self.a.default_playoffs, self.a.base_config, self.a.base_scoreset,
                            da.draft_board.TIER_GAP_Z)
        self.settings_apply()

    def _set_columns(self):
        """The table's columns for the current board: the scored stats (and the schedule columns)
        follow the scoring and playoff settings, so they are re-laid after each rebuild."""
        self.schedule = [c for c in SCHEDULE_HEADINGS if c in self.a.board.columns]
        self.stats = [c for c in STAT_HEADINGS if c in self.a.board.columns]
        self.columns = ([c for c in COLUMNS if c[0] != "injury" or "injury" in self.a.board.columns]
                        + [(c, SCHEDULE_HEADINGS[c], 50, "e") for c in self.schedule]
                        + [(c, STAT_HEADINGS[c], 50, "e") for c in self.stats])
        keys = [c[0] for c in self.columns]
        self.sheet.set_sheet_data([[""] * len(keys)], reset_col_positions=True, redraw=False)
        self.sheet.headers([heading for _, heading, *_ in self.columns], redraw=False)
        self.sheet.set_column_widths([width for _, _, width, _ in self.columns])
        self.sheet.align_columns({i: anchor for i, (*_, anchor) in enumerate(self.columns)},
                                 align_header=True, redraw=False)
        if self.sort_key not in keys:
            self.sort_key, self.sort_reverse = "rank", False

    def _build_rankings(self):
        frame = ttk.Frame(self.tabs)
        self.tabs.add(frame, text="Rankings")
        # A read-only spreadsheet (tksheet): unlike a ttk.Treeview it colours single cells.
        self.sheet = Sheet(frame, show_row_index=False, show_top_left=False, font=TABLE_FONT,
                           header_font=("Segoe UI", 9, "bold"), default_row_height=22,
                           table_bg="white")
        self.sheet.enable_bindings("single_select", "row_select", "column_width_resize",
                                   "arrowkeys", "copy")
        self.row_pids = []
        self._header_press = None
        self.sheet.bind("<ButtonPress-1>", self._header_down, add="+")
        self.sheet.bind("<ButtonRelease-1>", self._header_up, add="+")
        self.sheet.bind("<Double-Button-1>", self._double_click, add="+")
        self.sheet.pack(fill="both", expand=True)
        self._set_columns()

    def _double_click(self, event):
        """Tk sends a second quick click as a double-click, not a press: on a header it is still
        a click (sorting the other way); on a row in --manual it picks the player."""
        self._header_down(event)
        if self.args.manual:
            self.pick_selected(event)

    def _header_down(self, event):
        self._header_press = ((event.x_root, self.sheet.identify_column(event))
                              if self.sheet.identify_region(event) == "header" else None)

    def _header_up(self, event):
        """A click on a header sorts by its column; a drag (resizing a column) does not."""
        press, self._header_press = self._header_press, None
        if (press is None or press[1] is None or self.sheet.identify_region(event) != "header"
                or abs(event.x_root - press[0]) > 3
                or self.sheet.identify_column(event) != press[1]):
            return
        if press[1] < len(self.columns):
            self.sort_by(self.columns[press[1]][0])

    def _build_board(self):
        outer = ttk.Frame(self.tabs)
        self.tabs.add(outer, text="Draft board")
        canvas = tk.Canvas(outer, highlightthickness=0, background="white")
        ys = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        xs = ttk.Scrollbar(outer, orient="horizontal", command=canvas.xview)
        canvas.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        ys.pack(side="right", fill="y")
        xs.pack(side="bottom", fill="x")
        canvas.pack(fill="both", expand=True)
        grid = tk.Frame(canvas, background="#d1d5db")
        canvas.create_window((0, 0), window=grid, anchor="nw")
        grid.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(-e.delta // 120, "units")
                        if self.tabs.index("current") == 1 else None)

        tk.Label(grid, text="Rd", background="#e5e7eb", width=4).grid(row=0, column=0, sticky="nsew",
                                                                      padx=1, pady=1)
        self.team_column = {}
        for j, team in enumerate(self.teams, 1):
            mine = team["id"] == self.my_team
            tk.Label(grid, text=team["name"], wraplength=100, width=14, height=2,
                     background=MINE_COLOUR if mine else "#e5e7eb",
                     foreground="white" if mine else "black",
                     font=("Segoe UI", 9, "bold")).grid(row=0, column=j, sticky="nsew", padx=1, pady=1)
            self.team_column[team["id"]] = j
        rounds = max((c["round"] for c in self.cells), default=0)
        self.board_labels = {}
        for r in range(1, rounds + 1):
            tk.Label(grid, text=str(r), background="#e5e7eb").grid(row=r, column=0, sticky="nsew",
                                                                   padx=1, pady=1)
        for c in self.cells:
            label = tk.Label(grid, width=14, height=2, wraplength=100, justify="center",
                             font=("Segoe UI", 8), background="white")
            label.grid(row=c["round"], column=self.team_column[c["team_id"]], sticky="nsew",
                       padx=(3 if c["team_id"] == self.my_team else 1), pady=1)
            self.board_labels[c["overall"]] = label

    def _build_sidebar(self, parent):
        side = ttk.Frame(parent, padding=(8, 0, 0, 0))
        self.next_var, self.slots_var, self.need_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        ttk.Label(side, textvariable=self.next_var, wraplength=300).pack(anchor="w")
        ttk.Label(side, textvariable=self.slots_var, wraplength=300).pack(anchor="w", pady=(4, 0))
        ttk.Label(side, textvariable=self.need_var, style="Warn.TLabel",
                  wraplength=300).pack(anchor="w", pady=(4, 0))

        def listing(title, height):
            ttk.Label(side, text=title, font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(10, 2))
            box = tk.Listbox(side, height=height, activestyle="none", font=("Segoe UI", 9))
            box.pack(fill="both", expand=True)
            return box
        self.roster_box = listing("Your roster", 18)
        self.recent_box = listing("Last picks", 10)
        self.runs_var = tk.StringVar()
        ttk.Label(side, textvariable=self.runs_var).pack(anchor="w", pady=(2, 0))
        self.unmatched_box = listing("Unmatched picks (check by hand)", 3)
        return side

    # -- data -----------------------------------------------------------------------------------
    def _poll(self):
        while True:
            try:
                self.updates.put(da.read_board(self.args))
            except Exception as error:                               # noqa: BLE001 - keep going
                self.updates.put(error)
            threading.Event().wait(self.args.poll)

    def _drain(self):
        latest = None
        while not self.updates.empty():
            latest = self.updates.get()
        if isinstance(latest, Exception):
            self.clock_var.set(f"Could not read the draft board ({latest}); retrying")
        elif latest is not None:
            cells = da.picks_from(latest)
            if self._made(cells) != self._made(self.cells):
                self.cells = cells
                self.refresh()
        self.root.after(1000, self._drain)

    def fetch_now(self):
        if self.args.manual:
            self.refresh()
            return
        try:
            self.cells = da.picks_from(da.read_board(self.args))
        except Exception as error:                                   # noqa: BLE001
            self.clock_var.set(f"Could not read the draft board ({error})")
            return
        self.refresh()

    @staticmethod
    def _made(cells):
        return [(c["overall"], c["platform_id"], c["name"]) for c in cells if c["name"] is not None]

    def current_cells(self):
        if self.args.manual:
            return da.manual_cells(self.board_json, self.manual_picks)
        return self.cells

    def pick_selected(self, event=None):
        if event is not None and self.sheet.identify_region(event) != "table":
            return
        row = self.sheet.identify_row(event) if event is not None else None
        if row is None:
            chosen = self.sheet.get_currently_selected()
            row = chosen.row if chosen else None
        if row is None or row >= len(self.row_pids):
            return
        pid = self.row_pids[row]
        if pid in self.state["taken"] or self.state["clock"] is None:
            return
        self.manual_picks.append((pid, self.a.names.get(pid, str(pid))))
        self.refresh()

    @staticmethod
    def _adp_name(platform):
        return ADP_NAMES.get(platform, platform.title())

    _platform_name = _adp_name

    def set_positions(self):
        """The aggregate workbook's "Site Used (POS)": rebuild the board on another platform's
        positions -- values over replacement, replacement levels and slot fits all move."""
        chosen = self.positions_choice.get()
        platform = next(p for p in self.a.eligibility_platforms if self._platform_name(p) == chosen)
        if platform != self.a.eligibility_platform:
            self.a.build_board(platform)
            self.refresh()

    def set_adp(self):
        chosen = self.adp_choice.get()
        self.a.adp_platform = next(p for p in self.a.adp_platforms if self._adp_name(p) == chosen)
        self.fill_rankings()

    def reset_view(self):
        self.sort_key, self.sort_reverse = "rank", False
        self.filter.set(FILTERS[0])
        self.show_taken.set(False)
        self.search.set("")
        self.fill_rankings()
        self.sheet.set_yview(0)
        self.sheet.set_xview(0)

    def undo(self):
        self.manual_picks = self.manual_picks[:-1]
        self.refresh()

    # -- redraw ---------------------------------------------------------------------------------
    def refresh(self):
        a = self.a
        cells = self.current_cells()
        s = self.state = a.state(cells, self.my_team)
        roster, clock = s["roster"], s["clock"]
        self.gap = draft_module._unfillable(roster, a.config, a.eligibility)
        picks_left = a.config.roster_size - len(roster)

        now = f"{dt.datetime.now():%H:%M:%S}"
        if clock is None:
            self.clock_var.set(f"The draft is complete.   ({now})")
            self.next_var.set("")
        else:
            yours = "   -- YOUR PICK" if clock["team_id"] == self.my_team else ""
            self.clock_var.set(f"Pick {clock['overall']} (round {clock['round']}): "
                               f"{clock['team']} on the clock{yours}   ({now})")
            nxt = s["mine"][:2]
            self.next_var.set("Your next picks: " + ", ".join(
                f"#{c['overall']} (in {c['overall'] - clock['overall']})" for c in nxt))
        open_slots = a.open_slots(roster)
        self.slots_var.set(f"Open starting slots: {', '.join(open_slots) if open_slots else 'none'}"
                           f"  --  {picks_left} picks left")
        self.need_var.set("Positional need: every remaining pick must fill a starting slot."
                          if 0 < self.gap >= picks_left else "")
        self.next_overall = s["mine"][0]["overall"] if s["mine"] else None

        self.roster_box.delete(0, "end")
        for pid in roster:
            row = a.board.loc[pid] if pid in a.board.index else None
            self.roster_box.insert("end", f"{a.names.get(pid, pid)}"
                                   + (f"  ({row['positions']}, VOR {row['vor']:.1f})"
                                      if row is not None else ""))
        self.recent_box.delete(0, "end")
        for c in reversed(s["made"][-10:]):
            pid = a.player_id(c)
            pos = "/".join(sorted(a.eligibility.get(pid, ()))) if pid else "?"
            self.recent_box.insert("end", f"#{c['overall']} {c['team']}: {c['name']} ({pos})")
        recent = s["made"][-2 * a.config.teams:]
        runs = {x: sum(x in a.eligibility.get(a.player_id(c) or -1, ()) for c in recent)
                for x in ("G", "D")}
        self.runs_var.set(f"Last two rounds: {runs['G']} goalies, {runs['D']} defencemen"
                          if s["made"] else "")
        self.unmatched_box.delete(0, "end")
        for c in s["unmatched"]:
            self.unmatched_box.insert("end", f"#{c['overall']} {c['team']}: {c['name']}")

        self.fill_rankings()
        self.fill_board(cells, clock)

    def rows(self):
        a, s = self.a, self.state
        out = []
        for pid, r in a.board.iterrows():
            taken = pid in s["taken"]
            fills = (not taken and self.gap > 0
                     and draft_module._improves(s["roster"], pid, a.config, a.eligibility, self.gap))
            adp = r[f"adp_{a.adp_platform}"]
            gone = (not taken and self.next_overall is not None and pd.notna(adp)
                    and adp < self.next_overall)
            injury = r.get("injury")
            out.append({"pid": pid, "rank": r["rank"], "player": r["player"],
                        "injury": injury if isinstance(injury, str) else None,
                        "periph_pct": r.get("periph_pct"),
                        "team": r["team"] if pd.notna(r["team"]) else "",
                        "positions": r["positions"], "value": r["value"], "vor": r["vor"],
                        "tier": r.get("tier") or None,
                        "fills": fills, "sources": r["sources"],
                        "adp": adp, "gone": gone,
                        "taken": taken, **{c: r[c] for c in self.schedule + self.stats}})
        return out

    def _keep(self, row):
        if row["taken"] and not self.show_taken.get():
            return False
        text = self.search.get().strip().lower()
        if text and text not in row["player"].lower():
            return False
        f = self.filter.get()
        positions = set(str(row["positions"]).split("/"))
        if f == "Fills my slot":
            return row["fills"]
        if f.startswith("F "):
            return bool(positions & {"C", "LW", "RW"})
        return f == "All" or f in positions

    def fill_rankings(self):
        if not hasattr(self, "state"):
            return
        rows = [r for r in self.rows() if self._keep(r)]
        key = self.sort_key

        def blank(r):
            v = r[key]
            return v is None or (not isinstance(v, (bool, str)) and pd.isna(v))

        def sort_value(r):
            if key == "injury":
                return INJURY_ORDER.get(r[key], len(INJURY_ORDER))
            if key == "tier":      # by the first-listed group's tier, then value within it
                return int(r[key].split()[0][1:]), -r["value"]
            return (not r[key]) if isinstance(r[key], bool) else r[key]
        present = sorted((r for r in rows if not blank(r)), key=sort_value,
                         reverse=self.sort_reverse ^ (key in DESCENDING))
        rows = present + [r for r in rows if blank(r)]       # blanks last either way

        keys = [c[0] for c in self.columns]
        pos_col, tier_col = keys.index("positions"), keys.index("tier")
        scales = {keys.index(c): _scale_points(self.a.board[c]) for c in self.schedule}
        data, taken, colours = [], [], {}     # colours: bg -> [(row, col)]
        for i, r in enumerate(rows):
            text = {
                "rank": r["rank"],
                "injury": f"\U0001fa79 {INJURY_LETTER.get(r['injury'], '?')}" if r["injury"] else "",
                "player": r["player"], "team": r["team"], "positions": r["positions"],
                "value": _num(r["value"], 1), "vor": _num(r["vor"], 1), "tier": r["tier"] or "",
                "periph_pct": "" if pd.isna(r["periph_pct"]) else f"{r['periph_pct']:.0f}%",
                "sources": r["sources"], "adp": _num(r["adp"], 1),
                "gone": "likely gone" if r["gone"] else "",
                **{c: _num(r[c]) for c in self.schedule},
                **{c: _num(r[c], 1) for c in self.stats}}
            data.append([text[k] for k in keys])
            if r["taken"]:
                taken.append(i)
                continue
            first = str(r["positions"]).split("/")[0]
            if first in POSITION_COLOURS:
                colours.setdefault(POSITION_COLOURS[first], []).append((i, pos_col))
            for col, points in scales.items():
                v = r[keys[col]]
                if points is not None and pd.notna(v):
                    colours.setdefault(_scale_colour(v, *points), []).append((i, col))
            if r["tier"]:
                n = int(r["tier"].split()[0][1:])
                colours.setdefault(TIER_COLOURS[(n - 1) % len(TIER_COLOURS)],
                                   []).append((i, tier_col))
        self.row_pids = [r["pid"] for r in rows]

        sheet = self.sheet
        sheet.dehighlight_all(redraw=False)
        sheet.set_sheet_data(data, reset_col_positions=False, redraw=False)
        if taken:
            sheet.highlight_rows(taken, bg=TAKEN_BG, fg=TAKEN_FG, redraw=False)
        for bg, cells in colours.items():
            sheet.highlight_cells(cells=cells, bg=bg, redraw=False)
        headings = []
        for k, heading, *_ in self.columns:
            if k == "adp":
                heading = f"ADP {self._adp_name(self.a.adp_platform)}"
            arrow = (" ▼" if self.sort_reverse ^ (k in DESCENDING) else " ▲") \
                if k == key else ""
            headings.append(heading + arrow)
        sheet.headers(headings, redraw=False)
        sheet.refresh()

    def sort_by(self, key):
        self.sort_reverse = not self.sort_reverse if key == self.sort_key else False
        self.sort_key = key
        self.fill_rankings()

    def fill_board(self, cells, clock):
        a = self.a
        for c in cells:
            label = self.board_labels.get(c["overall"])
            if label is None:
                continue
            if c["name"] is None and c["platform_id"] is None:
                on_clock = clock is not None and c["overall"] == clock["overall"]
                label.configure(text=f"#{c['overall']}" + ("\nON THE CLOCK" if on_clock else ""),
                                background=CLOCK_COLOUR if on_clock else "white",
                                foreground="#6b7280")
                continue
            pid = a.player_id(c)
            positions = sorted(a.eligibility.get(pid, ())) if pid is not None else []
            first = next((p for p in ("C", "LW", "RW", "D", "G") if p in positions), None)
            label.configure(text=f"{c['name']}\n{'/'.join(positions) or '?'}",
                            background=POSITION_COLOURS.get(first, "#fecaca"), foreground="black")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    da.add_league_arguments(p)
    p.add_argument("--poll", type=int, default=10, help="Seconds between reads of the live board")
    p.add_argument("--manual", action="store_true", help="Double-click players instead of the API")
    p.add_argument("--replay-season", default=None,
                   help="Rehearse on a finished draft of this league, e.g. 2025")
    p.add_argument("--replay-seconds", type=float, default=5.0, help="Seconds per replayed pick")
    p.add_argument("--replay-start", type=int, default=0, help="Picks already made when it starts")
    da.add_standalone_arguments(p)
    args = p.parse_args()
    da.resolve_league(args, p)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    print("Building the board...")
    assistant = da.Assistant(args)
    board_json = da.open_board(args, assistant)
    my_team, my_name = da.team_id_for(board_json, args.team or "My team")
    root = tk.Tk()
    DraftWindow(root, assistant, args, board_json, my_team, my_name)
    root.mainloop()


if __name__ == "__main__":
    main()

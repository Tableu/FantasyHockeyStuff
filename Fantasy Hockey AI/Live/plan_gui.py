#!/usr/bin/env python
"""The daily plan, in a window -- the in-season counterpart of draft_gui.py, one league per run.

    python plan_gui.py                              # league beagles, today
    python plan_gui.py --league <name>
    python plan_gui.py --date 2026-09-29 --league-file fixtures/beagles/fake_league.json   # rehearsal

Nothing is scheduled: the plan runs when the window opens and while it stays open (planpass.py,
the same steps run_live.py takes):

    Full refresh    fresh injury, line-chart and goalie reports -> tonight's projections -> read the
                    league from its platform -> the shipped manager's plan. Runs on opening.
    Quick refresh   goalie reports -> tonight's projections -> the plan again (lineup news, late
                    scratches) -- what the auto window runs.
    Auto window     while the window is open, a quick refresh about 30 minutes before each group of
                    games starts (Fleaflicker locks each player at his own game), once per group.

Every run is also saved as reports/<league>/plans/plan_{date}_{time}.md + .json. Recommend-only:
make the moves on the platform yourself.

    Tonight      tonight's lineup from the roster you hold now: expected points, chance he plays /
                 starts, puck time (local), injury / GTD flag, a lock once his game has started, and
                 who the plan would put in that slot after its moves
    Moves        IR moves, adds and drops, claims -- with the rate each was priced on
    Roster       every player you hold now: status, rate, points tonight, games left this week, and
                 the recommended action (drop, move to IR, ...)
    Free agents  the best available now by rate (sortable), the recommended adds marked

The tables show the league as it stands; the moves are only recommendations until you make them.
    Matchup      the week so far, P(win), the opponent's roster
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

import seasonlayer  # noqa: F401 -- puts Season/ on sys.path; see seasonlayer.py
import leagues
import live
import planpass

AUTO_CHECK_MS = 60_000          # how often the auto window looks at the clock
PLAN_COLOUR = "#e0ecff"         # a row the plan recommends acting on
STATUS_COLOURS = {"OUT": "#fde2e2", "SUSP": "#fde2e2", "DTD": "#fff4d6", "GTD": "#fff4d6"}


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
        self.fa_sort, self.fa_reverse = "rate", True

        root.title(f"Plan -- {self.league.name}: {self.league.team_name or 'my team'}")
        root.geometry("1400x820")
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=22)
        style.configure("Big.TLabel", font=("Segoe UI", 12, "bold"))
        style.configure("Warn.TLabel", foreground="#b91c1c", font=("Segoe UI", 10, "bold"))

        self._build_toolbar()
        body = ttk.PanedWindow(root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.tabs = ttk.Notebook(body)
        body.add(self.tabs, weight=4)
        self.tonight = self._table("Tonight", [("slot", "Slot", 60), ("player", "Player", 240),
                                               ("mean", "Exp. pts", 80), ("sd", "SD", 60),
                                               ("p", "P(plays/starts)", 110), ("puck", "Puck", 90),
                                               ("flag", "Flag", 70), ("lock", "", 40),
                                               ("after", "After moves", 240)])
        self.moves = self._table("Moves", [("kind", "Move", 110), ("add", "Add", 260), ("add_rate", "pts/g", 70),
                                           ("drop", "Drop", 260), ("drop_rate", "pts/g", 70), ("note", "Note", 200)])
        player_columns = [("player", "Player", 240), ("positions", "Pos", 80), ("status", "Status", 70),
                          ("rate", "Rate (pts/g)", 90), ("per_game", "Tonight's proj.", 100),
                          ("plays_tonight", "Plays tonight", 95), ("games_left", "Games left", 80),
                          ("where", "", 90), ("plan", "Recommended", 110)]
        self.roster = self._table("Roster", player_columns)
        self.free_agents = self._table("Free agents", player_columns, sortable=True)
        self._build_matchup()
        body.add(self._build_sidebar(body), weight=1)

        root.after(200, self._drain)
        root.after(AUTO_CHECK_MS, self._auto_check)
        self.run("full")

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

    def _table(self, title, columns, sortable=False):
        frame = ttk.Frame(self.tabs)
        self.tabs.add(frame, text=title)
        tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", selectmode="browse")
        for key, heading, width in columns:
            tree.heading(key, text=heading, command=(lambda k=key: self._sort_fa(k)) if sortable else "")
            tree.column(key, width=width, anchor="w" if key in ("player", "add", "drop", "note", "slot", "after") else "center")
        for status, colour in STATUS_COLOURS.items():
            tree.tag_configure(status, background=colour)
        tree.tag_configure("plan", background=PLAN_COLOUR)
        tree.tag_configure("locked", foreground="#6b7280")
        tree.tag_configure("empty", foreground="#9ca3af")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        return tree

    def _build_matchup(self):
        frame = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(frame, text="Matchup")
        self.matchup_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.matchup_var, style="Big.TLabel", justify="left").pack(anchor="w")
        ttk.Label(frame, text="Opponent's roster", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(12, 2))
        self.opponent = ttk.Treeview(frame, columns=("player", "positions", "rate", "games_left"),
                                     show="headings", height=20)
        for key, heading, width in (("player", "Player", 260), ("positions", "Pos", 80),
                                    ("rate", "Rate (pts/g)", 100), ("games_left", "Games left", 90)):
            self.opponent.heading(key, text=heading)
            self.opponent.column(key, width=width, anchor="w" if key == "player" else "center")
        self.opponent.pack(fill="both", expand=True)

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
                planpass.snapshots(kinds, self.echo)
                for kind in kinds:
                    self.last[kind] = dt.datetime.now()
            planpass.tonight(self.day, self.echo)
            self.last["projections"] = dt.datetime.now()
            if self.runner is None:
                self.echo("building the board and the sampler (once a day)...")
                self.runner = live.LiveRunner(self.day, self.league)
            snapshot = planpass.read_league(self.league, self.day, args.league_file, args.platform_season, self.echo)
            self.last["league read"] = dt.datetime.now()
            plan = planpass.plan(self.runner, snapshot, now, self.echo)
            planpass.save(self.league, plan, self.day, f"{now:%H%M}", self.echo)
            self.messages.put(("plan", plan))
        except planpass.NoGames as error:
            self.messages.put(("nogames", str(error)))
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
            elif kind == "nogames":
                self.echo(payload)
                self.headline.set(f"{self.league.team_name or self.league.name} -- {self.day}: no NHL games")
                self._finished("No games today")
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
                          f"P(win) {p['p_win']:.0%} · moves left after the plan {p['moves_left']}")

        self.tonight.delete(*self.tonight.get_children())
        for s in p["lineup_now"]:
            after = s.get("after_moves") or ""
            if s["player"] is None:
                self.tonight.insert("", "end", values=(s["slot"], "(empty)", "", "", "", "", "", "", after),
                                    tags=("plan",) if after else ("empty",))
                continue
            tags = tuple(t for t in (s.get("flag"),) if t in STATUS_COLOURS) + (("locked",) if s.get("locked") else ())
            self.tonight.insert("", "end", tags=tags + (("plan",) if after else ()), values=(
                s["slot"], s["player"], _num(s["mean"]), _num(s["sd"]),
                "" if s["p_plays"] is None else f"{s['p_plays']:.0%}", local_time(s["puck_utc"]),
                s["flag"] or "", "\U0001f512" if s.get("locked") else "", after))
        for name in p["bench_now"]:
            self.tonight.insert("", "end", values=("BN", name, "", "", "", "", "", "", ""), tags=("empty",))

        self.moves.delete(*self.moves.get_children())
        for name in p["ir_to"]:
            self.moves.insert("", "end", values=("IR: move to", name, "", "", "", ""))
        for name in p["ir_off"]:
            self.moves.insert("", "end", values=("IR: activate", name, "", "", "", ""))
        for m in p["moves"]:
            note = f"reported {m['add_status']}" if m.get("add_status") not in (None, "ACTIVE") else ""
            self.moves.insert("", "end", values=(m["kind"].capitalize(), m["add"], _num(m["add_rate"]),
                                                 m["drop"] or "", _num(m["drop_rate"]), note))
        for c in p["claims"]:
            self.moves.insert("", "end", values=("Waiver claim", c["claim"], "", c["drop"] or "", "", ""))
        for name in p["other_drops"]:
            self.moves.insert("", "end", values=("Drop", "", "", name, "", ""))
        if not self.moves.get_children():
            self.moves.insert("", "end", values=("", "No moves today.", "", "", "", ""), tags=("empty",))

        self._fill_players(self.roster, p["roster"], where=True)
        self._fill_free_agents()

        self.matchup_var.set(f"Week {p['week']}: {p['team']} {p['my_week_points']:.1f}  vs  "
                             f"{opponent} {p['opponent_week_points']:.1f}\n"
                             f"P(win this week) {p['p_win']:.0%}  (z {p['matchup_z']:+.2f})")
        self.opponent.delete(*self.opponent.get_children())
        for r in sorted(p["opponent_roster"], key=lambda r: -r["rate"]):
            self.opponent.insert("", "end", values=(r["player"], r["positions"], _num(r["rate"]), r["games_left"]))

        self.goalie_box.delete(0, "end")
        for g in p["goalies"]:
            self.goalie_box.insert("end", f"{g['player']}: {g['p_start']:.0%}" + (f"  ({g['note']})" if g["note"] else ""))
        self.watch_box.delete(0, "end")
        for w in p["watch"]:
            self.watch_box.insert("end", f"{w['player']}: {w['status']}" + (f" -- {w['note']}" if w["note"] else ""))
        self.problem_var.set("\n".join(p["problems"]))

    def _fill_players(self, tree, rows, where=False):
        tree.delete(*tree.get_children())
        for r in rows:
            place = ("IR" if r["on_ir"] else "lineup" if r["in_lineup"] else "bench") if where else \
                    ("on waivers" if r["on_waivers"] else "")
            tags = ("plan",) if r.get("plan") else (r["status"],) if r["status"] in STATUS_COLOURS else ()
            tree.insert("", "end", tags=tags, values=(
                r["player"], r["positions"], r["status"] or "", _num(r["rate"]), _num(r["per_game"]),
                "" if r["plays_tonight"] is None else f"{r['plays_tonight']:.0%}", r["games_left"], place,
                _action(r.get("plan"))))

    def _sort_fa(self, key):
        self.fa_reverse = not self.fa_reverse if key == self.fa_sort else key in (
            "rate", "per_game", "plays_tonight", "games_left")
        self.fa_sort = key
        self._fill_free_agents()

    def _fill_free_agents(self):
        if not self.plan:
            return
        key = "player" if self.fa_sort == "where" else self.fa_sort
        text = key in ("player", "positions", "status", "plan")
        rows = sorted(self.plan["free_agents"], key=lambda r: (r[key] or "") if text else (r[key] is None, r[key] or 0),
                      reverse=self.fa_reverse)
        self._fill_players(self.free_agents, rows)

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

#!/usr/bin/env python
"""Draft-night assistant: follows the league's live draft and shows the best available players.

    python draft_assistant.py --team "Burnaby Beagles"            # follow the live draft
    python draft_assistant.py --team "Burnaby Beagles" --once     # one snapshot, then exit
    python draft_assistant.py --team "Burnaby Beagles" --manual   # the API is down: type picks
    python draft_assistant.py --standalone --slot 10 --league espn-default --weights espn-default         --eligibility espn --adp espn                              # any platform (ESPN): type picks

`--standalone` needs no draft API at all: the pick order comes from --teams (default: the league
file), --slot, --rounds (default: the roster size) and --order (snake). Saved draft-window settings
are per league (Settings/draft_window-<league>.json), so one league's scoring never reaches another.

**Read only.** It reads Fleaflicker's public draft board every `--poll` seconds and never submits a
pick. The board is `draft_board.py`'s: value over replacement on the external sources' consensus,
on Fleaflicker's positions for this league (`import_fantasy_fleaflicker.py`), fixed before the
draft. Each redraw shows who is on the clock and how many picks until yours, the best available by
VOR, whether each would fill one of your open starting slots, and ADP beside it -- used only for
"is he likely gone before your next pick", which is what ADP forecasts.

A pick is named by Fleaflicker's own player id (`Fantasy.PlatformPlayerIDs`, exported by
`ModelFeatures/build_players.py`), with an exact-name fallback; anything still unmatched is listed,
never dropped. Writes the latest redraw to reports/draft_assistant.md.
"""

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
import urllib.request

import pandas as pd

import draft_board
import league as league_module
import paths
import simlayer
from decisionlayer import draft as draft_module
from decisionlayer import load_strategy
from decisionlayer import slots as slots_module

log = logging.getLogger("draft-assistant")
API = "https://www.fleaflicker.com/api"
PLATFORM = "Fleaflicker"     # the league's platform: picks arrive by its player ids
# The aggregate workbook's roster slots, as this league's slot codes. W (a wing: LW or RW) is the
# workbook's; the league file does not define it, so it is added here. F/D is its UTIL(F/D).
ROSTER_SLOTS = ("C", "LW", "RW", "W", "F", "D", "F/D", "G")
EXTRA_SLOT_POSITIONS = {"W": ["LW", "RW"]}


def load_overrides(league: str = "league") -> dict:
    """The draft window's saved settings for `league` (paths.draft_window_settings), or {}."""
    path = paths.draft_window_settings(league)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


# A rehearsal of the live path (--replay-season): every poll really reads Fleaflicker, but reads a
# finished draft of this league (e.g. the 2025 one) and hides the picks not yet "made", revealing
# one every `seconds` from `start` on -- so polling, parsing, matching and redraws all run as they
# will on draft night.
REPLAY = {"season": None, "seconds": 5.0, "start": 0, "t0": None}


def configure_replay(season, seconds=5.0, start=0):
    REPLAY.update(season=season, seconds=float(seconds), start=int(start), t0=None)


def fetch_board(league_id: int) -> dict:
    url = f"{API}/FetchLeagueDraftBoard?sport=NHL&league_id={league_id}"
    if REPLAY["season"]:
        url += f"&season={REPLAY['season']}"
    with urllib.request.urlopen(url, timeout=20) as response:
        board = json.load(response)
    if REPLAY["season"]:
        if REPLAY["t0"] is None:
            REPLAY["t0"] = time.time()
        made = REPLAY["start"] + int((time.time() - REPLAY["t0"]) / REPLAY["seconds"])
        for row in board.get("rows", []):
            for cell in row.get("cells", []):
                if cell["slot"]["overall"] > made:
                    cell.pop("player", None)
    return board


def playoff_window(league_id: int, weeks: int):
    """First and last day of the league's fantasy playoffs: its last `weeks` scoring periods on
    Fleaflicker's schedule (league 12090: weeks 24-26, 2027-03-15 to 2027-04-04)."""
    url = f"{API}/FetchLeagueScoreboard?sport=NHL&league_id={league_id}"
    with urllib.request.urlopen(url, timeout=20) as response:
        periods = json.load(response)["eligibleSchedulePeriods"]

    def day(bound):     # the period boundaries are early-morning UTC instants on the NHL's day
        return dt.datetime.fromtimestamp(int(bound["startEpochMilli"]) / 1000,
                                         dt.timezone(dt.timedelta(hours=-5))).date()
    last = sorted(periods, key=lambda p: p["ordinal"])[-weeks:]
    return pd.Timestamp(day(last[0]["low"])), pd.Timestamp(day(last[-1]["high"]))


def picks_from(board_json: dict) -> list:
    """Every pick cell in draft order: overall, round, team id, team name, and the picked player's
    Fleaflicker id and name (None until he is picked)."""
    cells = []
    for row in board_json.get("rows", []):
        for cell in row.get("cells", []):
            player = (cell.get("player") or {}).get("proPlayer") or {}
            cells.append({"overall": cell["slot"]["overall"], "round": cell["slot"]["round"],
                          "team_id": cell["team"]["id"], "team": cell["team"]["name"],
                          "fleaflicker_id": player.get("id"), "name": player.get("nameFull")})
    return sorted(cells, key=lambda c: c["overall"])


class Assistant:
    def __init__(self, args):
        self.args = args
        strategy = self.strategy = load_strategy(args.strategy)
        season = args.season
        self.prior = args.prior_season or draft_board.previous(season)
        # Defaults: the league and scoring files, Fleaflicker's playoff weeks, the strategy's
        # platforms. The draft window's saved settings (Settings/draft_window.json) override them,
        # and a command-line flag overrides both.
        self.base_config = league_module.load(args.league)
        self.base_scoreset = simlayer.load_scoreset(args.weights)
        self.overrides = load_overrides(args.league)
        self.default_playoffs = None
        if not getattr(args, "standalone", False):   # standalone: no platform to ask
            try:
                self.default_playoffs = playoff_window(getattr(args, "league_id", 12090),
                                                       self.base_config.playoff_weeks)
            except Exception as error:                               # noqa: BLE001 - optional
                log.warning("no playoff weeks from Fleaflicker (%s)", error)
        saved = self.overrides.get("playoffs")
        self.playoffs = (tuple(pd.Timestamp(d) for d in saved) if saved
                         else self.default_playoffs)
        if self.playoffs is None:
            log.warning("no playoff dates: no off-night or playoff games")
        else:
            log.warning("fantasy playoffs %s to %s", self.playoffs[0].date(), self.playoffs[1].date())
        self.eligibility_platforms = sorted(
            p.name[len("fantasy_positions_"):-len(f"_{season}.parquet")]
            for p in paths.FEATURES_DIR.glob(f"fantasy_positions_*_{season}.parquet"))
        self.build_board(getattr(args, "eligibility", None)
                         or self.overrides.get("eligibility_platform")
                         or strategy.eligibility_platform)
        ids = pd.read_parquet(paths.platform_ids())
        ids = ids[(ids["platform"] == PLATFORM) & (ids["season"] == season)]
        self.by_fleaflicker_id = dict(zip(ids["external_id"].astype(str), ids["player_id"].astype(int)))
        players = pd.read_parquet(paths.players())
        counts = players["name"].value_counts()
        unique = players[players["name"].map(counts) == 1]
        self.by_name = dict(zip(unique["name"], unique["player_id"].astype(int)))
        self.names = dict(zip(players["player_id"].astype(int), players["name"]))
        self.adp_platforms = [c[len("adp_"):] for c in self.board.columns if c.startswith("adp_")]
        self.adp_platform = (getattr(args, "adp", None) or self.overrides.get("adp_platform")
                             or strategy.adp_platform).lower()
        if self.adp_platform not in self.adp_platforms:
            raise SystemExit(f"no {self.adp_platform} ADP for {season} (have: "
                             f"{', '.join(self.adp_platforms) or 'none'}); run ModelFeatures "
                             f"build_fantasy_positions.py --platform {self.adp_platform} "
                             f"--season {season}, or set draft.adp_platform")
        self.manual_picks = []

    def build_board(self, platform):
        """(Re)build the board on `platform`'s positions (draft.eligibility_platform) -- values,
        replacement levels, and which slots each player fills all follow the positions."""
        platform = platform.lower()
        if platform not in self.eligibility_platforms:
            raise SystemExit(f"no {platform} positions for {self.args.season} (have: "
                             f"{', '.join(self.eligibility_platforms) or 'none'}); run ModelFeatures "
                             f"build_fantasy_positions.py --platform {platform} --season "
                             f"{self.args.season}, or set draft.eligibility_platform")
        self.board, self.levels, self.config, self.eligibility = draft_board.build(
            self.args.season, self.prior, self.args.league, self.args.weights,
            pd.Timestamp(self.args.draft_date), self.strategy, eligibility_platform=platform,
            playoffs=self.playoffs, config=self.league_config(), scoreset=self.scoreset())
        self.eligibility_platform = platform
        self.slot_order = self.config.slot_order()

    def league_config(self):
        """The league file's config with the saved roster settings (teams, slots, bench) on it."""
        roster = self.overrides.get("roster")
        if not roster:
            return self.base_config
        from dataclasses import replace
        slots = {s: int(n) for s, n in roster["slots"].items() if int(n) > 0}
        return replace(self.base_config, teams=int(roster["teams"]), active_slots=slots,
                       bench=int(roster["bench"]),
                       slot_positions={**self.base_config.slot_positions, **EXTRA_SLOT_POSITIONS})

    def scoreset(self):
        """The scoring file's weights, or the saved ones (a stat at 0 is not scored)."""
        scoring = self.overrides.get("scoring")
        if not scoring:
            return self.base_scoreset
        return simlayer.scoring_module.ScoreSet({
            "name": "draft-window",
            "skaters": {k: float(v) for k, v in scoring["skaters"].items() if float(v)},
            "goalies": {k: float(v) for k, v in scoring["goalies"].items() if float(v)}})

    def save_overrides(self):
        path = paths.draft_window_settings(self.args.league)
        path.write_text(json.dumps(self.overrides, indent=2) + "\n", encoding="utf-8")
        return path

    # -- matching -----------------------------------------------------------------------------
    def player_id(self, pick):
        if pick.get("player_id") is not None:       # a typed pick (--manual) already knows who
            return pick["player_id"]
        if pick["fleaflicker_id"] is not None:
            found = self.by_fleaflicker_id.get(str(pick["fleaflicker_id"]))
            if found is not None:
                return found
        return self.by_name.get(pick["name"]) if pick["name"] else None

    # -- the state of the draft ---------------------------------------------------------------
    def state(self, cells, my_team):
        taken, unmatched, rosters = set(), [], {}
        for c in cells:
            if c["name"] is None and c["fleaflicker_id"] is None:
                continue
            pid = self.player_id(c)
            if pid is None:
                unmatched.append(c)
            else:
                taken.add(pid)
                rosters.setdefault(c["team_id"], []).append(pid)
        upcoming = [c for c in cells if c["name"] is None and c["fleaflicker_id"] is None]
        mine = [c for c in upcoming if c["team_id"] == my_team]
        return {"taken": taken, "unmatched": unmatched, "roster": rosters.get(my_team, []),
                "clock": upcoming[0] if upcoming else None, "mine": mine,
                "made": [c for c in cells if c not in upcoming]}

    def open_slots(self, roster):
        lineup = slots_module.assign(self.slot_order, {p: 1.0 for p in roster}, self.eligibility,
                                     self.config.accepts)
        return [self.slot_order[j] for j in lineup.unfilled]

    # -- the redraw -------------------------------------------------------------------------------
    def render(self, cells, my_team, my_name):
        s = self.state(cells, my_team)
        roster, clock = s["roster"], s["clock"]
        gap = draft_module._unfillable(roster, self.config, self.eligibility)
        picks_left = self.config.roster_size - len(roster)
        lines = [f"# Draft assistant -- {my_name} ({dt.datetime.now():%H:%M:%S})", ""]
        if clock is None:
            lines.append("The draft is complete.")
        else:
            nxt = s["mine"][:2]
            until = [c["overall"] - clock["overall"] for c in nxt]
            lines.append(f"On the clock: pick {clock['overall']} (round {clock['round']}), "
                         f"{clock['team']}." + (f"  **YOUR PICK.**" if clock["team_id"] == my_team else ""))
            if nxt:
                lines.append("Your next picks: " + ", ".join(
                    f"#{c['overall']} (in {u})" for c, u in zip(nxt, until)))
        open_slots = self.open_slots(roster)
        lines.append(f"Open starting slots: {', '.join(open_slots) if open_slots else 'none'}"
                     f" -- {picks_left} picks left")
        if gap > 0 and gap >= picks_left:
            lines.append("**Positional need: every remaining pick must fill a starting slot.**")
        lines.append("")

        next_overall = s["mine"][0]["overall"] if s["mine"] else None
        available = self.board[[p not in s["taken"] for p in self.board.index]]
        lines += ["## Best available", "",
                  f"| # | player | team | pos | value | VOR | fills a slot | sources | basis | ADP ({self.adp_platform}) | by your next pick |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        for i, (pid, r) in enumerate(available.head(self.args.top).iterrows(), 1):
            fills = draft_module._improves(roster, pid, self.config, self.eligibility, gap) if gap > 0 else False
            adp = r.get(f"adp_{self.adp_platform}")
            likely = ("likely gone" if next_overall is not None and pd.notna(adp)
                      and adp < next_overall else "")
            lines.append(f"| {i} | {r['player']} | {r['team'] if pd.notna(r['team']) else ''} | "
                         f"{r['positions']} | {r['value']:.0f} | {r['vor']:.0f} | "
                         f"{'yes' if fills else ''} | {r['sources']} | {r['basis']} | "
                         f"{'' if pd.isna(adp) else f'{adp:.0f}'} | {likely} |")
        lines += ["", "## Your roster", ""]
        for pid in roster:
            row = self.board.loc[pid] if pid in self.board.index else None
            lines.append(f"- {self.names.get(pid, pid)}"
                         + (f" ({row['positions']}, VOR {row['vor']:.0f})" if row is not None else ""))
        if not roster:
            lines.append("- (none yet)")
        made = s["made"][-10:]
        if made:
            lines += ["", "## Last picks", ""]
            for c in reversed(made):
                pid = self.player_id(c)
                pos = "/".join(sorted(self.eligibility.get(pid, ()))) if pid else "?"
                lines.append(f"- #{c['overall']} {c['team']}: {c['name']} ({pos})")
            recent = s["made"][-2 * self.config.teams:]
            runs = {x: sum(x in self.eligibility.get(self.player_id(c) or -1, ()) for c in recent)
                    for x in ("G", "D")}
            lines.append(f"\nLast two rounds: {runs['G']} goalies, {runs['D']} defencemen taken.")
        if s["unmatched"]:
            lines += ["", "## Unmatched picks (not removed from the board -- check by hand)", ""]
            lines += [f"- #{c['overall']} {c['team']}: {c['name']} (Fleaflicker id {c['fleaflicker_id']})"
                      for c in s["unmatched"]]
        return "\n".join(lines) + "\n"

    def show(self, text):
        if not self.args.once:
            os.system("cls" if os.name == "nt" else "clear")
        print(text)
        paths.ensure(paths.REPORTS_DIR)
        (paths.REPORTS_DIR / "draft_assistant.md").write_text(text, encoding="utf-8")


def team_id_for(board_json, name):
    """A team by its name, or by its Fleaflicker team id (Burnaby Beagles is 63341; the id stays
    the same across seasons while the name may not -- in 2025 it was One if by Landeskog)."""
    for team in board_json.get("draftOrder", []):
        if (team["name"].strip().lower() == name.strip().lower()
                or str(team["id"]) == name.strip()):
            return team["id"], team["name"]
    names = ", ".join(t["name"] for t in board_json.get("draftOrder", []))
    raise SystemExit(f"no team named {name!r} in this draft; teams: {names}")


def standalone_board(teams: int, slot: int, rounds: int, order: str = "snake",
                     my_name: str = "My team") -> dict:
    """A draft board for a league the tools cannot read (ESPN, Yahoo, a room with no API), in the
    shape Fleaflicker's FetchLeagueDraftBoard returns, so everything downstream -- picks_from,
    team_id_for, manual_cells, the redraw -- runs unchanged. Seat `slot` (1-based) is mine; the
    others are "Team N". `order` is "snake" (reverses every round) or "linear"."""
    if not 1 <= slot <= teams:
        raise SystemExit(f"--slot must be between 1 and {teams}")
    seats = [{"id": seat, "name": my_name if seat == slot else f"Team {seat}"}
             for seat in range(1, teams + 1)]
    rows, overall = [], 0
    for round_number in range(1, rounds + 1):
        seats_this_round = seats if order == "linear" or round_number % 2 == 1 else seats[::-1]
        cells = []
        for team in seats_this_round:
            overall += 1
            cells.append({"slot": {"overall": overall, "round": round_number}, "team": team})
        rows.append({"cells": cells})
    return {"draftOrder": seats, "rows": rows}


def add_standalone_arguments(p) -> None:
    """--standalone and its draft shape, shared by draft_assistant.py and draft_gui.py."""
    p.add_argument("--standalone", action="store_true",
                   help="Any platform: no draft API, you record picks (implies --manual); needs --slot")
    p.add_argument("--slot", type=int, default=None, help="--standalone: your draft slot, 1-based")
    p.add_argument("--teams", type=int, default=None, help="--standalone: teams (default: the league file)")
    p.add_argument("--rounds", type=int, default=None,
                   help="--standalone: rounds (default: the league's roster size)")
    p.add_argument("--order", default="snake", choices=("snake", "linear"), help="--standalone: pick order")


def open_board(args, assistant) -> dict:
    """The draft board to follow: Fleaflicker's (live or replayed), or the standalone one."""
    if not getattr(args, "standalone", False):
        return fetch_board(args.league_id)
    if args.slot is None:
        raise SystemExit("--standalone needs --slot (your draft slot, 1-based)")
    args.manual = True
    config = assistant.config
    return standalone_board(args.teams or config.teams, args.slot, args.rounds or config.roster_size,
                            args.order, args.team or "My team")


def manual_cells(board_json, picks):
    """The board's cells with the typed picks filled in, in order, as the API would show them."""
    cells = picks_from(board_json)
    for cell, (pid, name) in zip([c for c in cells if c["name"] is None], picks):
        cell["name"], cell["fleaflicker_id"], cell["player_id"] = name, None, pid
    return cells


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--league-id", type=int, default=12090)
    p.add_argument("--team", default=None,
                   help="Your team's name as Fleaflicker shows it (required unless --standalone)")
    p.add_argument("--season", default="2026-27")
    p.add_argument("--prior-season", default=None)
    p.add_argument("--league", default="league")
    p.add_argument("--weights", default="points-league")
    p.add_argument("--strategy", default=None)
    p.add_argument("--draft-date", default=dt.date.today().isoformat())
    p.add_argument("--adp", default=None, help="Whose ADP to show (default: draft.adp_platform)")
    p.add_argument("--eligibility", default=None,
                   help="Whose positions to use (default: draft.eligibility_platform)")
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--poll", type=int, default=10, help="Seconds between reads of the live board")
    p.add_argument("--once", action="store_true", help="Show one snapshot and exit")
    p.add_argument("--manual", action="store_true", help="Type picks instead of reading the API")
    p.add_argument("--replay-season", default=None,
                   help="Rehearse on a finished draft of this league, e.g. 2025")
    p.add_argument("--replay-seconds", type=float, default=5.0, help="Seconds per replayed pick")
    p.add_argument("--replay-start", type=int, default=0, help="Picks already made when it starts")
    add_standalone_arguments(p)
    args = p.parse_args()
    if not args.standalone and not args.team:
        p.error("--team is required (or use --standalone)")
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    if args.replay_season:
        configure_replay(args.replay_season, args.replay_seconds, args.replay_start)
    assistant = Assistant(args)
    board_json = open_board(args, assistant)
    my_team, my_name = team_id_for(board_json, args.team or "My team")

    if args.manual:
        picks = []
        while True:
            assistant.show(assistant.render(manual_cells(board_json, picks), my_team, my_name))
            typed = input("Pick made (player name, 'undo', or blank to quit): ").strip()
            if not typed:
                return
            if typed == "undo":
                picks = picks[:-1]
                continue
            matches = [pid for pid, name in assistant.names.items() if name.lower() == typed.lower()]
            if len(matches) != 1:
                print(f"{len(matches)} players named {typed!r}; type the full name as listed")
                time.sleep(2)
                continue
            picks.append((matches[0], assistant.names[matches[0]]))

    last = None
    while True:
        try:
            board_json = fetch_board(args.league_id)
        except Exception as error:                                   # noqa: BLE001 - keep going
            print(f"could not read the draft board ({error}); retrying in {args.poll}s")
            time.sleep(args.poll)
            continue
        cells = picks_from(board_json)
        made = sum(1 for c in cells if c["name"] is not None or c["fleaflicker_id"] is not None)
        if made != last:
            assistant.show(assistant.render(cells, my_team, my_name))
            last = made
        if args.once or made == len(cells):
            return
        time.sleep(args.poll)


if __name__ == "__main__":
    main()

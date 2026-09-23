#!/usr/bin/env python
"""How much of the free-agent pool's value is actually recoverable?

The ladder found every transaction policy net-negative in the target format, which reads as
"streaming does not work". That reading is wrong, and this script is what separates the two
possible causes:

    the pool is barren                      -> nothing to find, streaming genuinely cannot pay
    the pool is rich and the ranking is bad -> value is there and the models are not finding it

The pool is demonstrably rich. The best undrafted skater in a median matchup week scores 26.1
fantasy points, against 20.7 for the 90th percentile of drafted players and 9.9 for the median
drafted player; 31% of the top 250 by realized points went undrafted even from an ex-post board.
So the question is not whether value exists but how much of it is findable in advance.

**`OracleStreamer` answers that by cheating.** It is handed the realized outcomes and streams with
perfect foresight over the same seven-move budget, the same roster rules and the same forward
window every honest rung uses. It is not a strategy and it cannot be played -- it is a ceiling:

    oracle  -  no-moves   =  the value streaming could capture with perfect information
    rung 4  -  no-moves   =  the value the modelling stack actually captures
    the gap between them  =  headroom, i.e. how much better a transaction model could get

It deliberately bypasses `view.py`, which exists to make exactly this impossible. That is why it
lives in its own script instead of `managers.py`, is named so it cannot be mistaken for a rung, and
is never seated by `ladder.py`.

    python headroom.py --weights points-league
"""

import argparse
import logging
from collections import defaultdict

import pandas as pd

import draft as draft_module
import engine as engine_module
import inputs
import league as league_module
import managers as managers_module
import schedule as schedule_module
import simlayer

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger("headroom")


def parse_args():
    parser = argparse.ArgumentParser(description="Measure the streaming headroom")
    parser.add_argument("--season", default="2025-26")
    parser.add_argument("--prior-season", default="2024-25")
    parser.add_argument("--weights", default="points-league")
    parser.add_argument("--replications", type=int, default=4)
    parser.add_argument("--decision-sims", type=int, default=120)
    parser.add_argument("--league", default=None)
    return parser.parse_args()


class NoMoves(managers_module.FullSystem):
    """Rung 4's lineup with the transaction half switched off -- the floor for this comparison."""

    name = "no-moves"
    rung = 90

    def transactions(self, view) -> None:
        return None


class OracleStreamer(managers_module.FullSystem):
    """NOT A STRATEGY. Streams with perfect foresight, to establish the ceiling.

    Identical to rung 4 in every respect except the number it ranks acquisitions by: instead of a
    projection it uses what the player *actually* scored over the forward window. The budget, the
    roster rules, the fieldability constraint and the drop logic are unchanged, so the difference
    against rung 4 is attributable to ranking quality and nothing else.
    """

    name = "ORACLE-streamer"
    rung = 99
    realized = None          # injected by `run`: {(date, player_id): points}
    calendar = None

    def _window_points(self, view, player_id) -> float:
        """What he really scored from today to the end of the forward window."""
        week = view.week
        if week is None:
            return 0.0
        last = self.calendar.weeks[min(week - 1 + self.horizon_weeks,
                                      len(self.calendar.weeks) - 1)].end
        total = 0.0
        for day in self.calendar.days:
            if day < view.day:
                continue
            if day > last:
                break
            total += self.realized.get((pd.Timestamp(day), int(player_id)), 0.0)
        return total

    def transactions(self, view) -> None:
        state = view._state
        if view.moves_left <= 0:
            return
        eligibility = state.eligibility
        roster = [p for p in view.roster if p not in view.ir]
        if not roster:
            return

        pool = [p for p in view.free_agents() if not view.on_waivers(p)]
        candidates = sorted(((self._window_points(view, p), p) for p in pool), reverse=True)
        candidates = [(v, p) for v, p in candidates if v > 0.0]

        while view.moves_left > 0 and candidates:
            gain, incoming = candidates.pop(0)
            roster = [p for p in view.roster if p not in view.ir]
            drops = sorted(roster, key=lambda p: self._window_points(view, p))
            outgoing = next(
                (d for d in drops
                 if self._fieldable([x for x in roster if x != d] + [incoming], eligibility)),
                None)
            if outgoing is None:
                continue
            if gain <= self._window_points(view, outgoing):
                break
            try:
                state.add(self.team_index, incoming, view.day, drop=outgoing, reason="oracle")
            except Exception:
                continue


def run(args):
    config = league_module.load(args.league)
    scoreset = simlayer.load_scoreset(args.weights)
    data = inputs.load_season(args.season)
    universe = pd.concat([data["projections"][["player_id", "position"]],
                          data["goalie_candidates"][["player_id", "position"]]]
                         ).drop_duplicates("player_id")
    eligibility = inputs.load_eligibility(config, universe)
    calendar = schedule_module.from_candidates(
        data["projections"][["game_id", "game_date", "team_id"]], config.week_starts_on)

    prior_actuals = inputs.load_actuals(args.prior_season)
    prior_goalies = inputs.load_goalie_starts(args.prior_season)
    board = {int(k): float(v) for k, v in
             draft_module.prior_season_board(prior_actuals, prior_goalies, scoreset).items()}
    rate = {int(k): float(v) for k, v in
            draft_module.prior_season_rate(prior_actuals, prior_goalies, scoreset).items()}

    # The oracle's cheat sheet, built from the same Outcomes the engine resolves nights with.
    outcomes = engine_module.Outcomes(data["actuals"], data["goalie_starts"], scoreset)
    OracleStreamer.realized = outcomes.points
    OracleStreamer.calendar = calendar

    managers_module.LADDER[90] = NoMoves
    managers_module.LADDER[99] = OracleStreamer

    rows = []
    for replication in range(args.replications):
        field = managers_module.build_field(config, scoreset, rungs=(90, 3, 4, 99),
                                            replication=replication)
        season = engine_module.Season(config, calendar, data, eligibility, scoreset, field,
                                     replication=replication, decision_sims=args.decision_sims)
        table = season.run(board, rate)["teams"]
        rows.append(table.assign(replication=replication))
    seats = pd.concat(rows, ignore_index=True)
    seats["pts_wk"] = seats["points"] / seats["weeks"]
    return config, seats


def report(config, seats, weights):
    names = {90: "no moves (floor)", 3: "rung 3 naive streamer",
             4: "rung 4 full system", 99: "ORACLE streamer (ceiling)"}
    g = seats.groupby("rung").agg(n=("pts_wk", "size"), pts=("pts_wk", "mean"),
                                  sd=("pts_wk", "std"),
                                  productive=("slot_nights_productive", "mean"),
                                  moves=("moves_spent", "mean"))
    g["se"] = g["sd"] / (g["n"] ** 0.5)

    print(f"\n{config.name}: {config.teams} teams, {weights}, "
          f"{seats.replication.nunique()} rotations\n")
    print(f"{'':>26} {'pts/week':>16} {'slot-nights':>12} {'moves':>7}")
    for rung in (90, 3, 4, 99):
        if rung in g.index:
            r = g.loc[rung]
            print(f"{names[rung]:>26} {r.pts:>9.1f} +/-{r.se:>4.1f} {r.productive:>12.0f}"
                  f" {r.moves:>7.0f}")

    pivot = seats.pivot_table(index="replication", columns="rung", values="pts_wk")
    print()
    if 90 in pivot:
        for rung, label in ((3, "rung 3"), (4, "rung 4"), (99, "ORACLE")):
            if rung in pivot:
                d = (pivot[rung] - pivot[90]).dropna()
                se = d.std() / (len(d) ** 0.5)
                print(f"  {label:>7} - no moves: {d.mean():+7.2f} +/- {se:4.2f}")
        if 99 in pivot and 4 in pivot:
            ceiling = (pivot[99] - pivot[90]).mean()
            captured = (pivot[4] - pivot[90]).mean()
            print(f"\n  streaming is worth up to {ceiling:+.1f} points a week with perfect "
                  f"information.")
            if ceiling > 0:
                print(f"  the modelling stack captures {captured:+.1f} of it "
                      f"({captured / ceiling:.0%}).")
                print(f"  headroom for a better transaction model: "
                      f"{ceiling - captured:.1f} points a week.")


def main():
    args = parse_args()
    config, seats = run(args)
    report(config, seats, args.weights)


if __name__ == "__main__":
    main()

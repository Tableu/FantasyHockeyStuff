#!/usr/bin/env python
"""Section 11: tune the shipped manager's parameters on 2024-25, then touch 2025-26 once.

    python tune.py                          # stages A, B and C on 2024-25, 14-team points
    python tune.py --stage A                # one stage
    python tune.py --final                  # the frozen winner against the shipped values on 2025-26

**What is tuned.** The shipped system is rung 17: the orchestrator (rung 7) drafting by the
consensus VOR board. A candidate is that manager with different add/drop and streaming parameters
-- nothing else, so the draft, the goalie prior and the playoff behaviour are the shipped ones
(`managers.build_field` refuses a candidate that differs elsewhere).

**Seat-paired.** Every draft is played twice: once as the shipped league (rungs 2, 5, 6 and 17)
and once with the candidate (rung 27: rung 17 on the candidate's parameters) in exactly the seats
rung 17 held. The draft does not depend on in-season parameters, so both runs have the same
rosters, draft slots and schedule, and a candidate's score is its seats' points a week minus the
same seats' in the shipped run, averaged per draft. What is left is the parameters' effect and its
knock-on effect through the shared wire, which a real change has too.

The first search seated the candidate beside the shipped system instead, in other seats. Seats
running the same manager in one draft differ by 7-16 points a week, so that design's error was
+/- 2-2.4 points a week at 8 drafts; seat-pairing measured +/- 0.6-1.1 on the same drafts
(2026-09-24). A candidate equal to the shipped values scores exactly 0.

**The search**, staged and one setting at a time except where settings interact:

    A  add/drop, streaming at the shipped values: horizon x rate crossed, then margin and the
       claim premium
    B  streaming, on A's winner: spots, reserve, lambda, rental margin, gate, flat, rental claims
    C  A's settings again, with B's winner in place

**Promotion.** Every candidate runs at 4 drafts and each stage's top 3 rerun at 8. The best of
them replaces the stage's reference only if it beats the reference by more than two paired
standard errors at 8 drafts (both measured against the shipped run on the same seats, so the
difference is paired by draft); otherwise the reference stays. Selection on noise is the failure this
rule exists to stop.

**2025-26 is refused** unless `--final`: it is the one clean holdout, and the final number is the
only thing it may be used for. Results are cached under reports/tune/, keyed by the parameters,
the season, the format and a hash of the code, so an interrupted search resumes and a code change
starts afresh. Writes docs/tuning-<season>.md.
"""

import argparse
import dataclasses
import hashlib
import json
import logging
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

import field as field_module
import inputs
import ladder
import league as league_module
import paths
import schedule as schedule_module
import simlayer
from decisionlayer import load_strategy
from decisionlayer import managers as managers_module

log = logging.getLogger("tune")
FINAL_SEASON = "2025-26"
SHIPPED, CAND = 17, managers_module.CANDIDATE
BASE_FIELD = (2, 5, 6, SHIPPED)            # the shipped league
CAND_FIELD = (2, 5, 6, CAND)               # the same league, the candidate in rung 17's seats
CODE_DIRS = ("Decisions", "Season", "Simulation", "Settings")


# ---------------------------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------------------------

def _plain(value):
    return None if value is None else ("inf" if isinstance(value, float) and math.isinf(value)
                                       else value)


def params_of(strategy) -> dict:
    return {"adddrop": {k: _plain(v) for k, v in dataclasses.asdict(strategy.adddrop).items()},
            "streaming": {k: _plain(v) for k, v in dataclasses.asdict(strategy.streaming).items()}}


def label(strategy) -> str:
    return f"{strategy.adddrop.describe()} | {strategy.streaming.describe()}"


def with_(base, adddrop=None, streaming=None):
    return dataclasses.replace(
        base, adddrop=dataclasses.replace(base.adddrop, **(adddrop or {})),
        streaming=dataclasses.replace(base.streaming, **(streaming or {})))


def stage_a(ref):
    """Add/drop: horizon x rate crossed (they interact), then margin and claim premium alone."""
    out = [with_(ref, adddrop={"horizon_weeks": h, "rate_source": r})
           for h in (1, 3, 6, None) for r in ("ros", "per_game")]
    out += [with_(ref, adddrop={"margin": m}) for m in (0.0, 0.5, 1.0, 1.5)]
    out += [with_(ref, adddrop={"claim_premium": c}) for c in (0.0, 5.0)]
    return out


def stage_b(ref):
    """Streaming, one setting at a time on the reference."""
    s = ref.streaming
    out = [with_(ref, streaming={"spots": k}) for k in (0, 1, 2, 3)]
    out += [with_(ref, streaming={"reserve": r}) for r in (0, 1, 2)]
    out += [with_(ref, streaming={"lam": v}) for v in (0.0, 1.0, 2.0, 3.0, 4.0)]
    out += [with_(ref, streaming={"margin": v}) for v in (0.0, 0.5)]
    out += [with_(ref, streaming={flag: not getattr(s, flag)}) for flag in ("gate", "flat", "claim")]
    return out


STAGES = {"A": stage_a, "B": stage_b, "C": stage_a}


def unique(candidates, ref):
    """Drop duplicates and the reference itself (it runs anyway, as the stage's baseline)."""
    seen, out = {json.dumps(params_of(ref), sort_keys=True)}, []
    for c in candidates:
        key = json.dumps(params_of(c), sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


# ---------------------------------------------------------------------------------------------
# Running one candidate
# ---------------------------------------------------------------------------------------------

def code_hash() -> str:
    digest = hashlib.sha1()
    for folder in CODE_DIRS:
        for path in sorted((paths.SIBLINGS / folder).rglob("*")):
            if path.suffix in (".py", ".json") and "reports" not in path.parts \
                    and "__pycache__" not in path.parts:
                digest.update(path.relative_to(paths.SIBLINGS).as_posix().encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


class Context:
    """Everything a run needs, loaded once per season and format."""

    def __init__(self, season, prior_season, league_name, scoring, strategy, workers,
                 field_config=None):
        self.season, self.league_name, self.scoring = season, league_name, scoring
        self.shipped = strategy
        self.config = league_module.load(league_name)
        self.data = inputs.load_season(season)
        self.data["prior_season"] = prior_season
        universe = pd.concat([self.data["projections"][["player_id", "position"]],
                              self.data["goalie_candidates"][["player_id", "position"]]]
                             ).drop_duplicates("player_id")
        self.eligibility = inputs.load_eligibility(self.config, universe)
        self.calendar = schedule_module.from_candidates(
            self.data["projections"][["game_id", "game_date", "team_id"]],
            self.config.week_starts_on, self.config.min_first_week_games)
        self.scoreset = simlayer.load_scoreset(scoring)
        self.data["prior"] = {self.scoreset.name: ladder.prior_season(prior_season,
                                                                      self.scoreset, strategy)}
        self.data["vor"] = {self.scoreset.name: ladder.vor_board(
            self.data, prior_season, self.scoreset, self.config, self.eligibility, strategy)}
        self.field = field_config or field_module.load()
        ladder.prepare_field(self.data, self.field, strategy)
        self.workers = workers
        self.code = code_hash()
        self.cache = paths.ensure(paths.REPORTS_DIR / "tune")

    def key(self, candidate, replications) -> str:
        payload = {"params": params_of(candidate), "season": self.season,
                   "league": self.league_name, "scoring": self.scoring,
                   "field": self.field.describe(),
                   "replications": replications, "code": self.code}
        return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]

    def _seats(self, rungs, candidate, replications) -> pd.DataFrame:
        args = SimpleNamespace(replications=replications, workers=self.workers,
                               verbose_weeks=False, decision_sims=200)
        runs = ladder.run_replications(args, self.config, self.calendar, self.data,
                                       self.eligibility, self.scoreset, rungs, self.shipped,
                                       candidate)
        _, teams = ladder.summarize(runs, self.scoreset.name)
        return (teams.assign(pts=teams["points"] / teams["weeks"],
                             win=teams["matchup_wins"] / teams["weeks"],
                             playoff=teams["made_playoffs"].astype(float))
                .set_index(["replication", "seat"])
                [["rung", "pts", "win", "playoff", "moves_spent", "rentals", "move_hit_rate"]])

    def base(self, replications) -> pd.DataFrame:
        """The shipped league at this many drafts, played once and cached."""
        path = self.cache / f"base_{self.key(self.shipped, replications)}.parquet"
        if path.exists():
            return pd.read_parquet(path)
        table = self._seats(BASE_FIELD, None, replications)
        table.to_parquet(path)
        return table

    def run(self, candidate, replications) -> dict:
        """Per-draft gaps of the candidate over the shipped system in the same seats, cached."""
        path = self.cache / f"{self.key(candidate, replications)}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        base = self.base(replications)
        if params_of(candidate) == params_of(self.shipped):
            gap = {m: [0.0] * replications for m in ("pts", "win", "playoff")}
            mine = base[base["rung"] == SHIPPED]
            result = {"label": label(candidate), "params": params_of(candidate),
                      "replications": replications, "gap": gap, "spill": 0.0,
                      "moves": float(mine["moves_spent"].mean()),
                      "rentals": float(mine["rentals"].mean()),
                      "hit_rate": float(mine["move_hit_rate"].mean())}
        else:
            alt = self._seats(CAND_FIELD, candidate, replications)
            mine = alt.index[alt["rung"] == CAND]
            if not (base.loc[mine, "rung"] == SHIPPED).all():
                raise AssertionError("the candidate's seats are not rung 17's in the shipped run")
            gap = {m: (alt.loc[mine, m] - base.loc[mine, m]).groupby(level="replication")
                   .mean().tolist() for m in ("pts", "win", "playoff")}
            rest = alt.index[alt["rung"] != CAND]
            result = {"label": label(candidate), "params": params_of(candidate),
                      "replications": replications, "gap": gap,
                      "spill": float((alt.loc[rest, "pts"] - base.loc[rest, "pts"]).abs().mean()),
                      "moves": float(alt.loc[mine, "moves_spent"].mean()),
                      "rentals": float(alt.loc[mine, "rentals"].mean()),
                      "hit_rate": float(alt.loc[mine, "move_hit_rate"].mean())}
        path.write_text(json.dumps(result, indent=1), encoding="utf-8")
        return result


def stats(values):
    v = np.asarray(values, dtype=float)
    return float(v.mean()), (float(v.std(ddof=1) / math.sqrt(len(v))) if len(v) > 1 else math.nan)


def versus(result, reference) -> tuple:
    """Candidate's points gap minus the reference's, paired by replication: (mean, se)."""
    return stats(np.subtract(result["gap"]["pts"], reference["gap"]["pts"]))


# ---------------------------------------------------------------------------------------------
# A stage
# ---------------------------------------------------------------------------------------------

def run_stage(ctx, name, ref, screen, confirm, top):
    """Screen every candidate at `screen` drafts, rerun the top at `confirm`, promote or keep."""
    candidates = unique(STAGES[name](ref), ref)
    log.info("stage %s: %d candidates around %s", name, len(candidates), label(ref))
    ref_screen = ctx.run(ref, screen)
    screened = []
    for i, c in enumerate(candidates, 1):
        r = ctx.run(c, screen)
        d, se = versus(r, ref_screen)
        log.info("  %s %2d/%d  %+6.2f ± %.2f  %s", name, i, len(candidates), d, se, r["label"])
        screened.append((d, c, r))
    screened.sort(key=lambda t: -t[0])

    ref_confirm = ctx.run(ref, confirm)
    confirmed = []
    for d, c, _ in screened[:top]:
        r = ctx.run(c, confirm)
        dd, se = versus(r, ref_confirm)
        wd, _ = stats(np.subtract(r["gap"]["win"], ref_confirm["gap"]["win"]))
        confirmed.append({"candidate": c, "result": r, "gap": dd, "se": se, "win": wd})
    best = max(confirmed, key=lambda e: e["gap"]) if confirmed else None
    promoted = best is not None and best["gap"] > 2 * best["se"]
    winner = best["candidate"] if promoted else ref
    log.info("stage %s: %s", name, f"PROMOTED {label(winner)} ({best['gap']:+.2f} ± {best['se']:.2f})"
             if promoted else "the reference stays")
    return {"name": name, "reference": ref, "ref_screen": ref_screen, "ref_confirm": ref_confirm,
            "screened": screened, "confirmed": confirmed, "promoted": promoted, "winner": winner}


# ---------------------------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------------------------

def stage_markdown(stage, screen, confirm):
    lines = [f"## Stage {stage['name']}\n",
             f"Reference: `{label(stage['reference'])}`. Gap = the candidate's points a week over "
             f"the shipped system in the same seats, minus the reference's, paired by draft. "
             f"'Others' is how far the rest of the league moved, points a week.\n",
             f"| candidate | gap at {screen} | ± | win-rate gap | moves | rentals | others |",
             "|---|---|---|---|---|---|---|"]
    for d, c, r in stage["screened"]:
        _, se = versus(r, stage["ref_screen"])
        w, _ = stats(np.subtract(r["gap"]["win"], stage["ref_screen"]["gap"]["win"]))
        lines.append(f"| `{r['label']}` | {d:+.2f} | {se:.2f} | {w:+.3f} | {r['moves']:.0f} | "
                     f"{r['rentals']:.0f} | {r.get('spill', 0):.1f} |")
    lines += ["", f"**Top {len(stage['confirmed'])} at {confirm} drafts:**", "",
              "| candidate | gap | ± | win-rate gap | flag |", "|---|---|---|---|---|"]
    for e in stage["confirmed"]:
        flag = "points and wins disagree" if e["gap"] * e["win"] < 0 else ""
        lines.append(f"| `{e['result']['label']}` | {e['gap']:+.2f} | {e['se']:.2f} | "
                     f"{e['win']:+.3f} | {flag} |")
    lines.append("")
    lines.append(f"**Result:** {'promoted `' + label(stage['winner']) + '`' if stage['promoted'] else 'nothing clears two standard errors; the reference stays'}.\n")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--season", default="2024-25")
    p.add_argument("--prior-season", default=None, help="Default: the season before")
    p.add_argument("--league", default="league")
    p.add_argument("--weights", default="points-league")
    p.add_argument("--stage", choices=("A", "B", "C", "all"), default="all")
    p.add_argument("--screen", type=int, default=4, help="Drafts per candidate in the screen")
    p.add_argument("--confirm", type=int, default=8, help="Drafts for each stage's top few")
    p.add_argument("--top", type=int, default=3)
    p.add_argument("--workers", type=int, default=None,
                   help="Processes (default: ladder.default_workers)")
    p.add_argument("--strategy", default=None)
    p.add_argument("--final", action="store_true",
                   help="Run the frozen parameters (--tuned) against the shipped on 2025-26")
    p.add_argument("--tuned", default=None, help="JSON of the tuned parameters (for --final)")
    return p.parse_args()


def previous(season):
    first = int(season[:4]) - 1
    return f"{first}-{str(first + 1)[2:]}"


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                        stream=sys.stderr)
    for noisy in ("inputs", "draft", "engine", "simlayer", "ladder", "schedule"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    season = FINAL_SEASON if args.final else args.season
    if season == FINAL_SEASON and not args.final:
        raise SystemExit(f"{FINAL_SEASON} is the final holdout: tune on another season, and use "
                         f"--final for the one comparison it is kept for")
    shipped = load_strategy(args.strategy)
    ctx = Context(season, args.prior_season or previous(season), args.league, args.weights,
                  shipped, args.workers)

    if args.final:
        if not args.tuned:
            raise SystemExit("--final needs --tuned, the parameters the 2024-25 search froze")
        tuned_params = json.loads(Path(args.tuned).read_text(encoding="utf-8"))
        unplain = lambda block: {k: math.inf if v == "inf" else v for k, v in block.items()}
        tuned = with_(shipped, adddrop=unplain(tuned_params["adddrop"]),
                      streaming=unplain(tuned_params["streaming"]))
        base, result = ctx.run(shipped, args.confirm), ctx.run(tuned, args.confirm)
        d, se = versus(result, base)
        print(f"{season} {args.league} {args.weights}: tuned − shipped {d:+.2f} ± {se:.2f} points "
              f"a week ({args.confirm} drafts) | {label(tuned)}")
        return

    stages = ["A", "B", "C"] if args.stage == "all" else [args.stage]
    ref, done = shipped, []
    for name in stages:
        stage = run_stage(ctx, name, ref, args.screen, args.confirm, args.top)
        done.append(stage)
        ref = stage["winner"]

    out = paths.ensure(paths.DOCS_DIR) / f"tuning-{season}.md"
    frozen = paths.REPORTS_DIR / "tune" / f"tuned_{season}_{args.league}_{args.weights}.json"
    frozen.write_text(json.dumps(params_of(ref), indent=1), encoding="utf-8")
    out.write_text(
        f"# Tuning on {season}: {args.league}, {args.weights}\n\n"
        f"Generated by `tune.py` (code {ctx.code}); see its docstring. Seat-paired: each draft "
        f"played as the shipped league (rungs 2, 5, 6, 17) and again with the candidate in rung "
        f"17's seats. "
        f"Shipped: `{label(shipped)}`. **Result: `{label(ref)}`**"
        f"{'' if params_of(ref) != params_of(shipped) else ' -- the shipped values'}.\n\n"
        + "\n".join(stage_markdown(s, args.screen, args.confirm) for s in done),
        encoding="utf-8")
    print(f"result: {label(ref)}\n-> {out}\n-> {frozen}")


if __name__ == "__main__":
    main()

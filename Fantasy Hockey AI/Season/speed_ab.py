#!/usr/bin/env python
"""Old-vs-new for a speed change: are the seasons identical, and how much faster is it?

    python speed_ab.py identity --old patches/old_fits.py   # drafts 0-2: every season output equal?
    python speed_ab.py time --old patches/old_fits.py       # one draft, one core, 3 runs each way

The "new" side is the code as it stands. The "old" side is the same code with a patch file run
over it -- a few lines that put the previous version back, e.g.:

    from decisionlayer import weekplan
    def fits_old(self, spent, day, cost): ...           # the previous implementation
    weekplan.WeekPlanner.fits = fits_old

The patch is executed at module level (SPEED_AB_PATCH), so every spawned worker process runs it
too -- a monkeypatch made only in the parent would not reach them. Patch module attributes the
callers look up through the module (`slots.assign_value`, `weekplan.WeekPlanner.fits`), not names
already imported elsewhere with `from ... import`.

**identity** plays drafts 0-2 of the shipped league (oneseat's layout, the strategy given) under
both sides and compares every season output -- teams, matchups, pwin, playoffs, transactions. A
speed change must leave them identical; anything else is a strategy change for oneseat.py.
**time** plays draft 0 on one core, three times each way, alternating, and prints the medians: run
it with the machine otherwise idle. Used for the sim-speed plan (2026-10-04/05): items 2-5 and the
matroid-greedy lineup total.
"""
import argparse
import os
import pickle
import statistics
import subprocess
import sys
import time

import paths  # noqa: F401  (Season's paths first, as oneseat imports it)

if os.environ.get("SPEED_AB_PATCH"):
    with open(os.environ["SPEED_AB_PATCH"], encoding="utf-8") as _patch:
        exec(compile(_patch.read(), os.environ["SPEED_AB_PATCH"], "exec"), {"__name__": "speed_ab_patch"})


def build(strategy):
    import oneseat
    sys.argv = ["oneseat.py", "--strategy", strategy]
    ctx, shipped, _, layout, _, _ = oneseat.setup(oneseat.parse_args())
    return ctx, shipped, layout


def play(strategy, indices, workers):
    import ladder
    from types import SimpleNamespace
    ctx, shipped, layout = build(strategy)
    args = SimpleNamespace(replications=len(indices), workers=workers, verbose_weeks=False,
                           decision_sims=200)
    return ladder.run_replications(args, ctx.config, ctx.calendar, ctx.data, ctx.eligibility,
                                   ctx.scoreset, layout, shipped, None, indices=indices)


def child(mode, which, args):
    """Run this script again for one side: the old side with the patch in its environment."""
    env = {k: v for k, v in os.environ.items() if k != "SPEED_AB_PATCH"}
    if which == "old":
        env["SPEED_AB_PATCH"] = os.path.abspath(args.old)
    command = [sys.executable, os.path.abspath(__file__), mode, "--strategy", args.strategy]
    return subprocess.run(command, check=True, capture_output=True, text=True, env=env).stdout


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("mode", choices=["identity", "time", "_identity", "_time"])
    p.add_argument("--old", help="Patch file that puts the old code back (required for identity / time)")
    p.add_argument("--strategy", default="strategy-beagles")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--out", help=argparse.SUPPRESS)
    args = p.parse_args()

    if args.mode == "_identity":                  # one side, in its own process
        with open(args.out, "wb") as handle:
            pickle.dump(play(args.strategy, [0, 1, 2], 3), handle)
        return
    if args.mode == "_time":
        build(args.strategy)
        t = time.perf_counter()
        play(args.strategy, [0], 1)
        print(f"{time.perf_counter() - t:.1f}")
        return
    if not args.old:
        raise SystemExit("--old patch file required")

    if args.mode == "identity":
        import tempfile
        results = {}
        for which in ("old", "new"):
            env = {k: v for k, v in os.environ.items() if k != "SPEED_AB_PATCH"}
            if which == "old":
                env["SPEED_AB_PATCH"] = os.path.abspath(args.old)
            out = os.path.join(tempfile.gettempdir(), f"speed_ab_{which}_{os.getpid()}.pkl")
            subprocess.run([sys.executable, os.path.abspath(__file__), "_identity", "--strategy",
                            args.strategy, "--out", out], check=True, capture_output=True, env=env)
            with open(out, "rb") as handle:
                results[which] = pickle.load(handle)
            os.remove(out)
        same = True
        for r, (a, b) in enumerate(zip(results["old"], results["new"])):
            for key in a:
                ok = a[key].equals(b[key]) if hasattr(a[key], "equals") else a[key] == b[key]
                if not ok:
                    same = False
                    print(f"draft {r}: {key} DIFFERS")
        print("identity over drafts 0-2 (teams, matchups, pwin, playoffs, transactions):",
              "IDENTICAL" if same else "DIFFERENT")
    else:
        times = {"old": [], "new": []}
        for _ in range(args.runs):
            for which in ("old", "new"):
                out = child("_time", which, args)
                times[which].append(float(out.strip().splitlines()[-1]))
        for which, ts in times.items():
            print(f"{which}: median {statistics.median(ts):.1f} s  ({', '.join(f'{t:.1f}' for t in ts)})")


if __name__ == "__main__":
    main()

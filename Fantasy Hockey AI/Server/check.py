#!/usr/bin/env python
"""The checks to run before a push, on this PC (Windows or the dev container):

    python check.py            # all three
    python check.py --fast     # the tests and the settings check, no rehearsal plan

1. tests       the offline suite (tests/: no platform, database or NHL feed; what CI runs)
2. settings    sync_league_settings.py --check: each hand-made league's saved platform answers
               translate to exactly its settings files
3. rehearsal   one real plan of beagles on its made-up league (fixtures/beagles/fake_league.json)
               with today's models, features and tonight's build -- what CI can't have; saved
               to a temporary reports folder (FH_REPORTS_DIR), so nothing real is touched

PASS/FAIL per step; exits 1 on any failure. Reads no league from its platform.
"""

import argparse
import datetime as dt
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def step(name, command, env=None, quiet=False):
    start = time.time()
    done = subprocess.run([sys.executable, *command], cwd=HERE, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          env={**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})})
    ok = done.returncode == 0
    lines = [line for line in (done.stdout + done.stderr).splitlines() if line.strip()]
    print(f"{'PASS' if ok else 'FAIL'}  {name}  ({time.time() - start:.0f} s)")
    if not ok or not quiet:
        tail = lines if not ok else lines[-1:]
        for line in tail[-25:]:
            print(f"      {line}")
    return ok


def rehearsal() -> bool:
    today = dt.date.today()
    with tempfile.TemporaryDirectory(prefix="fh-check-") as reports:
        ok = step("rehearsal plan (beagles on fixtures/beagles/fake_league.json)",
                  ["planpass.py", "--league", "beagles", "--league-file", "fixtures/beagles/fake_league.json",
                   "--date", today.isoformat(), "--now", f"{today.isoformat()}T18:00:00"],
                  env={"FH_REPORTS_DIR": reports})
        saved = list(Path(reports).glob("beagles/plans/plan_*.json"))
        if ok and not saved:
            print("FAIL  rehearsal plan: no plan saved")
            return False
        return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fast", action="store_true", help="Skip the rehearsal plan")
    args = parser.parse_args()
    results = [step("tests", ["-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"]),
               step("settings fixtures", ["sync_league_settings.py", "--check"])]
    if not args.fast:
        results.append(rehearsal())
    print("all passed" if all(results) else "FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())

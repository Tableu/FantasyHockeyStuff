# Server

The phone app's server (Fantasy Hockey Phone App Plan). Today: the Linux test environment of
Phase 1 -- the plan steps run in a container capped like the Azure VM (2 vCPU, 1 GiB RAM + 2 GiB
swap) against this PC's SQL Server -- and the server (Phase 1.5): server.py, the plan refreshes
behind a small HTTP API, which the plan window (Live/plan_gui.py) shows.

## The server

    python server.py                    # league beagles, today, http://127.0.0.1:8000
    python server.py --date 2026-09-29 --league-file ../Live/fixtures/beagles/fake_league.json         --skip-snapshots --no-auto      # rehearsal

| Call | Does |
| --- | --- |
| GET /plan | The day's newest saved plan: {file, saved_at, plan} (404 before the first) |
| POST /refresh {"mode": "full" or "quick"} | Starts a refresh and returns its job; a tap while one runs joins it |
| GET /jobs/{id}?after=n | The job's state and its progress lines after line n |
| GET /status | The day, the running job, when each step last ran, the newest plan's file and time |

The refreshes are planpass.Planner's -- the same steps run_live.py takes -- and save the same plan files.
The auto window (a quick refresh ~30 minutes before each group of games) runs in the server unless
--no-auto. No login yet, so it listens on this PC only (in the container: --host 0.0.0.0, which
compose publishes to 127.0.0.1 alone).

## Running it

Needs Docker Desktop (it installs WSL2).

    cd "Fantasy Hockey AI/Server"
    docker compose up -d --build        # the container, running server.py (localhost:8000 on Windows)
    docker compose logs -f server       # its output
    docker compose exec server bash     # a shell in it; the repo is /app
    cd /app/pipeline && python snapshot_live.py --kind injuries --dry-run
    docker compose down                 # stops it (otherwise it comes back with Docker Desktop)

A rehearsal server (--date, --league-file, --skip-snapshots, --no-auto) runs by hand in the shell
on another port, e.g. `python server.py --host 0.0.0.0 --port 8001 ...`, which compose does not
publish -- or stop the container and run server.py on Windows, where plan_gui.py finds it at
127.0.0.1:8000.

- **Code and data** are not in the image: the repo is mounted at /app, so an edit on Windows runs
  in the container at once. Reading the mounted Windows folder is slower than a Linux disk, so
  timings here are an upper bound for the VM.
- **Database:** the same credentials files as on Windows; `NHLSTATS_DB_SERVER=host.docker.internal`
  (compose.yaml) replaces their `localhost`, read by pipeline/nhl_pipeline/config.py and
  ModelFeatures/nhlstats_db.py. SQL Server must accept TCP on 1433 with SQL logins (it does,
  checked 2026-10-02).
- **Time zone:** America/Vancouver, so "today" is the same day as on the PC.
- **Port 8000** is published to this PC only (127.0.0.1), for the plan window:
  `python plan_gui.py` (in Live/) shows the server's newest plan, its buttons start the server's
  refreshes and stream their progress, and it follows the server's auto refreshes. The window
  needs the server running.
- Measured 2026-10-02: a full refresh 36 s, a quick one 21 s.

# Server

The phone app's server (Fantasy Hockey Phone App Plan). Today: the Linux test environment of
Phase 1 -- the plan steps run in a container capped like the Azure VM (2 vCPU, 1 GiB RAM + 2 GiB
swap) against this PC's SQL Server -- and the server (Phase 1.5): server.py, the plan refreshes
behind a small HTTP API, which the plan window (Live/plan_gui.py) shows.

## The server

    python server.py                    # every active league (beagles, espn-la), today, http://127.0.0.1:8000
    python server.py --league beagles   # one league; repeat --league for several
    python server.py --league beagles --date 2026-09-29 --league-file ../Live/fixtures/beagles/fake_league.json --skip-snapshots --no-auto      # rehearsal

One server plans every league. A refresh takes the snapshots and tonight's projections once, then
reads, plans and saves each league in turn (one league failing leaves the others' plans), and the
auto window re-plans every league. Every call but the job's takes `?league=<name>`, defaulting to
the first league served; plan_gui.py sends its own `--league`.

| Call | Does |
| --- | --- |
| GET /plan | The league's newest saved plan for the plan's day -- today, or tomorrow once every game today has started (today's until tomorrow's is saved): {league, file, saved_at, plan} (404 before the first) |
| POST /refresh {"mode": "full" or "quick"} | Starts a refresh of every league and returns its job; a tap while one runs joins it |
| GET /jobs/{id}?after=n | The job's state, each league's saved plan file, its progress lines after line n |
| GET /status | The leagues served, the day, the running job, when each step last ran (with the league's own read), its newest plan |
| GET /games | Today's games: score, clock, how many of your players and your opponent's are in each |
| GET /goals | Every goal today, newest first, with the fantasy points it earned either side |
| GET /games/{id}?after=n | One game: line score, team stats, box score with fantasy points, plays after sortOrder n |
| GET /games/{id}/lines | Each team's lines, pairs and power-play / penalty-kill units as used |
| GET /droppable | The players you marked OK to drop: {league, player_ids}; [] = the model chooses its own drops |
| PUT /droppable {"player_ids": [...]} | Replaces that list ([] clears it); the next refresh plans on it (Live/droppable.py) |

The live games calls are games.py's: the NHL's public feeds held in memory (nothing goes to the
database), one shared copy per feed for every league, fetched again only once the NHL's cache says it expired (about
20 s) and only when someone asks. The lines come from the NHL's HTML time-on-ice reports (about a
minute behind; asked with If-Modified-Since, which they honour with a 304) and the nightly lineup
build's clustering. Live, the strength each second comes from who the reports put on the ice, not
the play-by-play's situation codes, which lag and misreport during a game. Fantasy owners come from
the newest plan and players.parquet's NHL ids.

The refreshes are planpass.Planner's -- the same steps run_live.py takes -- and save the same plan files.
The auto window (a quick refresh ~30 minutes before each group of games, and one once today's last
game has started) runs in the server unless --no-auto. From that last puck on, the plan is
tomorrow's: its moves, lineup and projections, since tonight has no lineup left to set. No login yet, so it listens on this PC only (in the container: --host 0.0.0.0, which
compose publishes to 127.0.0.1 alone).

## Running it

Needs Docker Desktop (it installs WSL2).

    cd "Fantasy Hockey AI/Server"
    docker compose up -d --build        # the container, running server.py (localhost:8000 on Windows)
    docker compose logs -f server       # its output
    docker compose exec server bash     # a shell in it; the repo is /app
    cd /app/pipeline && python snapshot_live.py --kind injuries --dry-run
    docker compose down                 # stops it (otherwise it comes back with Docker Desktop)

Memory: the leagues are planned in a child process per refresh (planpass.plan_in_child), which
exits after, so the server stays the same size however many leagues it plans; it held about 480 MiB
with two leagues planned in its own process (2026-10-02), of which under 1 MB per league was
anything it kept.

A rehearsal server (--league, --date, --league-file, --skip-snapshots, --no-auto) runs by hand in the shell
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
- **measure_steps.py** times each refresh and nightly step in its own process with its peak memory
  (the plan's Phase 0 table): `docker compose exec server python measure_steps.py`. Linux only.
  2026-10-02 in the container: every step ran; the largest was build_tonight on a late-season day
  (34 s, 417 MB), and the container peaked at 635 MiB with no swap.

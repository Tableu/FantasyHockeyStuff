#!/usr/bin/env python
"""A Fleaflicker league's transaction history -- how real managers use the wire.

The test league's opponents are our own code: rung 2 never moves, rungs 5 and 6 barely do. Real
managers in this league family stream hard (sister league 12088, 2023-24..2025-26: ~5 pickups a
team-week, half of all team-weeks at the 7-move cap), and a realistic opponent model has to be
calibrated on what they actually do. This reads it from the league's public transaction log.

**Why the HTML log and not the JSON API.** `FetchLeagueTransactions` answers only for recent
history and refused requests (HTTP 403) after a burst of calls, which also took the live tools'
move counts with it for a while. The public page `/nhl/leagues/<id>/transactions` pages with
`tableOffset` (15 entries a page) and filters with `teamId`. **The league-wide log caps at about
1,425 entries** -- deeper offsets repeat the last page -- **but each team's own log reaches back
years** (to 2021 in 12088), so this walks team by team.

**Politeness.** One request every `--pause` seconds (default 4), every page cached under
`data/raw/fleaflicker_<league>/`, and a cached page is never fetched again -- a rerun only asks for
what it does not have. `--refresh-recent N` refetches each team's newest N pages, for an in-season
update.

Each entry carries the Fleaflicker player id in its link (`/players/<slug>-<id>`), which maps to
our PlayerID through `platform_ids.parquet` (build_players.py), and an exact UTC timestamp.

    python build_fleaflicker_transactions.py --league 12088 --since 2023-07-01
    python build_fleaflicker_transactions.py --league 12090 --since 2026-07-01 --refresh-recent 3
    python build_fleaflicker_transactions.py --league 12088 --summary       # re-parse the cache only

Writes data/features/fleaflicker_transactions_<league>.parquet: one row per entry (draft picks,
adds, claims, cuts, trades...), with `season` and the Monday-start `week_start` it fell in.
"""

import argparse
import datetime as dt
import html
import logging
import re
import time
import urllib.request

import numpy as np
import pandas as pd

import paths

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fleaflicker-transactions")

SITE = "https://www.fleaflicker.com"
PAGE_SIZE = 15

# One entry: the icon block opens it; the next one (or the end of the list) closes it.
ENTRY = re.compile(r'<div class="list-group-item with-icon clearfix">(.*?)(?=<div class="list-group-item with-icon clearfix">|</div></div><div class="clearfix"></div>|<ul class="pagination|$)', re.S)
PLAYER = re.compile(r'<a class="player-text" href="/nhl/leagues/\d+/players/[^"]*?-(\d+)">([^<]+)</a>')
POSITION = re.compile(r'<span class="position"[^>]*>([^<]+)</span>')
NHL_TEAM = re.compile(r'<span class="player-team">([^<]+)</span>')
FANTASY_TEAM = re.compile(r'<a href="/nhl/leagues/\d+/teams/(\d+)">([^<]+)</a>')
WHEN = re.compile(r'<relative-time datetime="([^"]+)"')
TEAM_LINK = re.compile(r'transactions\?tableOffset=0&amp;teamId=(\d+)')


def parse_args():
    p = argparse.ArgumentParser(description="Import a Fleaflicker league's transaction history")
    p.add_argument("--league", type=int, required=True, help="Fleaflicker league id")
    p.add_argument("--since", default="2023-07-01",
                   help="Walk each team's log back to this date (default 2023-07-01)")
    p.add_argument("--pause", type=float, default=4.0, help="Seconds between requests (default 4)")
    p.add_argument("--refresh-recent", type=int, default=0, metavar="N",
                   help="Refetch each team's newest N pages even if cached (in-season updates)")
    p.add_argument("--summary", action="store_true",
                   help="Parse the cache and print the behaviour summary; fetch nothing")
    return p.parse_args()


class Fetcher:
    def __init__(self, league, pause):
        self.league, self.pause = league, pause
        self.cache = paths.ensure(paths.DATA_DIR / "raw" / f"fleaflicker_{league}")
        self.requests = 0

    def page(self, offset, team=None, refresh=False, offline=False) -> str | None:
        name = f"team{team}_offset_{offset:06d}.html" if team else f"league_offset_{offset:06d}.html"
        path = self.cache / name
        if path.exists() and not refresh:
            return path.read_text(encoding="utf-8")
        if offline:
            return None
        url = f"{SITE}/nhl/leagues/{self.league}/transactions?tableOffset={offset}"
        if team:
            url += f"&teamId={team}"
        if self.requests:
            time.sleep(self.pause)
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8", "replace")
        self.requests += 1
        path.write_text(body, encoding="utf-8")
        return body


def entries(body: str) -> list:
    """Every transaction entry on one page."""
    out = []
    start = body.find('<div id="body-center-main">')
    for block in ENTRY.findall(body[start:] if start >= 0 else body):
        players, when = PLAYER.findall(block), WHEN.search(block)
        team = FANTASY_TEAM.search(block)
        if not players or when is None or team is None:
            continue
        # The verb sits between the fantasy team and the player: "added", "cut", "claimed",
        # "drafted", or a link ("traded for"). Tags stripped, whatever it is.
        rest = block[team.end():]
        action = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", rest[:rest.find('<div class="player">')])).strip()
        position, nhl = POSITION.search(block), NHL_TEAM.search(block)
        out.append({
            "time_utc": pd.Timestamp(when.group(1)).tz_convert(None) if pd.Timestamp(when.group(1)).tzinfo
                        else pd.Timestamp(when.group(1)),
            "team_id": int(team.group(1)), "team_name": html.unescape(team.group(2)),
            "action": html.unescape(action) or "unknown",
            "fleaflicker_id": int(players[0][0]), "player_name": html.unescape(players[0][1]),
            "position": position.group(1) if position else None,
            "nhl_team": nhl.group(1) if nhl else None,
        })
    return out


def team_ids(fetcher, offline) -> list:
    body = fetcher.page(0, offline=offline)
    if body is None:
        return sorted({int(p.name.split("_")[0][4:]) for p in fetcher.cache.glob("team*_offset_*.html")})
    return sorted({int(t) for t in TEAM_LINK.findall(body)})


def walk_team(fetcher, team, since, refresh_recent, offline) -> list:
    """One team's log, newest first, back to `since` or until the log runs out (pages repeat)."""
    rows, offset, previous = [], 0, None
    while True:
        body = fetcher.page(offset, team, refresh=offset < refresh_recent * PAGE_SIZE, offline=offline)
        if body is None:
            break
        page = entries(body)
        key = tuple((e["time_utc"], e["fleaflicker_id"], e["action"]) for e in page)
        if not page or key == previous:
            log.info("team %d: log ends at offset %d", team, offset)
            break
        rows += page
        if page[-1]["time_utc"] < since:
            log.info("team %d: back to %s at offset %d", team, page[-1]["time_utc"].date(), offset)
            break
        previous, offset = key, offset + PAGE_SIZE
    return rows


def season_of(when: pd.Series) -> pd.Series:
    start = np.where(when.dt.month >= 8, when.dt.year, when.dt.year - 1)
    return pd.Series([f"{y}-{str(y + 1)[2:]}" for y in start], index=when.index)


def attach_player_ids(table: pd.DataFrame) -> pd.DataFrame:
    ids = pd.read_parquet(paths.FEATURES_DIR / "platform_ids.parquet")
    ids = ids[ids["platform"] == "Fleaflicker"].copy()
    ids["fleaflicker_id"] = pd.to_numeric(ids["external_id"], errors="coerce")
    ids = ids.dropna(subset=["fleaflicker_id"]).drop_duplicates("fleaflicker_id")
    mapping = dict(zip(ids["fleaflicker_id"].astype(int), ids["player_id"].astype(int)))
    table["player_id"] = table["fleaflicker_id"].map(mapping).astype("Int64")
    return table


def summary(table: pd.DataFrame) -> None:
    """Pickups per team-week by season -- the numbers the opponent model is calibrated on."""
    t = table[table["action"].isin(["added", "claimed"])]
    t = t[(t["time_utc"].dt.month >= 10) | (t["time_utc"].dt.month <= 4)]
    for season, g in t.groupby("season"):
        per = g.groupby(["team_id", "week_start"]).size().unstack(fill_value=0)
        per = per.reindex(columns=sorted(g["week_start"].unique()), fill_value=0)
        if per.shape[1] < 10:
            continue
        v = per.to_numpy()
        early = g["time_utc"].dt.month.isin([10, 11, 12])
        rate = lambda s: len(s) / max(s["week_start"].nunique() * per.shape[0], 1)
        print(f"{season}: {len(g)} pickups, {per.shape[0]} teams, {per.shape[1]} weeks | per team-week "
              f"mean {v.mean():.2f} median {np.median(v):.0f}, at 7+ {(v >= 7).mean():.0%}, "
              f"zero {(v == 0).mean():.0%} | team means {per.mean(axis=1).min():.1f}-"
              f"{per.mean(axis=1).max():.1f} | Oct-Dec {rate(g[early]):.2f} Jan-Apr {rate(g[~early]):.2f} "
              f"| goalies {g['position'].fillna('').str.contains('G').mean():.0%} "
              f"claims {(g['action'] == 'claimed').mean():.0%}")


def main():
    args = parse_args()
    fetcher = Fetcher(args.league, args.pause)
    since = pd.Timestamp(args.since)
    rows = []
    for team in team_ids(fetcher, offline=args.summary):
        rows += walk_team(fetcher, team, since, args.refresh_recent, offline=args.summary)
    if not rows:
        raise SystemExit("no entries -- nothing cached and nothing fetched")
    table = (pd.DataFrame(rows)
             .drop_duplicates(["team_id", "time_utc", "fleaflicker_id", "action"])
             .sort_values("time_utc").reset_index(drop=True))
    table.insert(0, "league_id", args.league)
    table["season"] = season_of(table["time_utc"])
    # League time: Fleaflicker's scoring day turns over at 6:00 AM Eastern, and a week runs
    # Monday to Sunday of those days (Live/platforms/fleaflicker.py).
    local = (table["time_utc"].dt.tz_localize("UTC").dt.tz_convert("America/New_York")
             .dt.tz_localize(None) - pd.Timedelta(hours=6))
    table["league_day"] = local.dt.normalize()
    table["week_start"] = table["league_day"] - pd.to_timedelta(local.dt.dayofweek, unit="D")
    table = attach_player_ids(table)
    out = paths.ensure(paths.FEATURES_DIR) / f"fleaflicker_transactions_{args.league}.parquet"
    table.to_parquet(out, index=False)
    log.info("%d entries (%d requests made), %s .. %s, player id matched %.1f%% -> %s",
             len(table), fetcher.requests, table["time_utc"].min().date(),
             table["time_utc"].max().date(), 100 * table["player_id"].notna().mean(), out.name)
    log.info("actions: %s", table["action"].value_counts().to_dict())
    summary(table)


if __name__ == "__main__":
    main()

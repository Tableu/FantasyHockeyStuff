#!/usr/bin/env python
"""CLI for downloading one or more NHL goal clips on demand. Built for an AI agent (or a
human) to invoke directly -- prints one JSON object per clip to stdout (JSONL: one line per
clip, in order, flushed as each finishes) so output is trivial to parse without scraping
human-readable text, whether you asked for one clip or a thousand.

This tool does one thing: given clip identifier(s), resolve each to a currently-valid MP4
URL (see nhl_pipeline/media/clip_resolver.py) and stream it to disk. It deliberately does
not do player/game name lookups -- find the GoalID(s) first (e.g. by querying NHLStats
directly: `SELECT GoalID, HighlightClipID FROM Game.Goals WHERE ...`), then pass them here.

Usage:
    python download_clip.py --clip-id 6387782787112
    python download_clip.py --clip-id 6387782787112 6393276846112 6387782214112
    python download_clip.py --goal-id 42
    python download_clip.py --goal-id 42 91 137 --type discrete
    python download_clip.py --goal-id - < goal_ids.txt        # one id per line, any count
    python download_clip.py --goal-id 42 --out "C:/clips/my_clip.mp4"   # single id only
    python download_clip.py --goal-id 42 91 137 --out-dir "C:/clips"    # batch destination
    python download_clip.py --clip-id 6387782787112 --overwrite

Output (stdout, one JSON object per line, in the same order as the requested ids):
    Freshly downloaded:
        {"status": "downloaded", "source_kind": "clip", "source_id": 6387782787112,
         "clip_id": 6387782787112, "name": "Caufield breaks the ice", "duration_seconds": 54.8,
         "file_path": "clips/6387782787112.mp4", "file_size_bytes": 14625845}
    Already on disk (--overwrite not given):
        {"status": "cached", "source_kind": "goal", "source_id": 42, "clip_id": 6387782787112,
         "file_path": "clips/6387782787112.mp4", "file_size_bytes": 14625845}
    That one clip failed (the rest of the batch still runs):
        {"status": "error", "source_kind": "goal", "source_id": 43, "message": "..."}

Exit code: 0 if every clip in the batch succeeded, 1 if any single one errored.
"""

import argparse
import json
import sys
from pathlib import Path

import requests

from nhl_pipeline import db
from nhl_pipeline.media import clip_resolver

DEFAULT_CLIPS_DIR = Path(__file__).resolve().parent / "clips"
DOWNLOAD_CHUNK_BYTES = 256 * 1024


def _collect_ids(values: list) -> list:
    """values is argparse's raw string list for --clip-id/--goal-id. A single '-' means
    read whitespace/newline-separated ids from stdin instead (for batches too large to
    comfortably fit on a command line)."""
    if values == ["-"]:
        return [int(tok) for tok in sys.stdin.read().split()]
    return [int(v) for v in values]


def resolve_one_clip_id(cursor, id_kind: str, id_value: int, clip_type: str) -> int:
    if id_kind == "clip":
        return id_value

    cursor.execute(
        "SELECT HighlightClipID, DiscreteClipID FROM Game.Goals WHERE GoalID = ?", id_value
    )
    row = cursor.fetchone()
    if row is None:
        raise ValueError(f"No Goals row with GoalID={id_value}")

    clip_id = row.HighlightClipID if clip_type == "highlight" else row.DiscreteClipID
    if clip_id is None:
        raise ValueError(f"GoalID={id_value} has no {clip_type} clip id recorded")
    return clip_id


def download(clip_id: int, dest_path: Path, overwrite: bool, policy_key: str) -> dict:
    if dest_path.exists() and not overwrite:
        return {
            "status": "cached",
            "clip_id": clip_id,
            "file_path": str(dest_path),
            "file_size_bytes": dest_path.stat().st_size,
        }

    data = clip_resolver.resolve_clip(clip_id, policy_key)
    mp4_url = None
    for source in data.get("sources", []):
        if source.get("container") == "MP4" and source.get("src", "").startswith("https"):
            mp4_url = source["src"]
            break
    if mp4_url is None:
        raise RuntimeError(f"No downloadable MP4 source found for clip {clip_id}")

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_name(dest_path.name + ".part")
    with requests.get(mp4_url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with open(tmp_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_BYTES):
                f.write(chunk)
    tmp_path.replace(dest_path)

    return {
        "status": "downloaded",
        "clip_id": clip_id,
        "name": data.get("name"),
        "duration_seconds": round(data.get("duration", 0) / 1000, 1),
        "file_path": str(dest_path),
        "file_size_bytes": dest_path.stat().st_size,
    }


def process_one(
    cursor, id_kind: str, id_value: int, clip_type: str,
    out_path_override, out_dir: Path, overwrite: bool, policy_key: str,
) -> dict:
    result = {"source_kind": id_kind, "source_id": id_value}
    try:
        clip_id = resolve_one_clip_id(cursor, id_kind, id_value, clip_type)
        dest_path = out_path_override or (out_dir / f"{clip_id}.mp4")
        result.update(download(clip_id, dest_path, overwrite, policy_key))
    except Exception as exc:
        result["status"] = "error"
        result["message"] = str(exc)
    return result


def main():
    parser = argparse.ArgumentParser(description="Download one or more NHL goal clips on demand")
    identifier = parser.add_mutually_exclusive_group(required=True)
    identifier.add_argument("--clip-id", nargs="+", metavar="ID",
                             help="One or more Brightcove clip ids, or '-' to read them from stdin")
    identifier.add_argument("--goal-id", nargs="+", metavar="ID",
                             help="One or more Game.Goals.GoalID values (looked up in NHLStats), or '-' for stdin")
    parser.add_argument("--type", choices=["highlight", "discrete"], default="highlight",
                         help="Which clip variant when using --goal-id (default: highlight)")
    parser.add_argument("--out", type=Path, default=None,
                         help="Exact destination file path -- only valid with a single id")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_CLIPS_DIR,
                         help="Destination directory for batch downloads (default: clips/); files are named <clip_id>.mp4")
    parser.add_argument("--overwrite", action="store_true", help="Re-download even if the file already exists")
    args = parser.parse_args()

    id_kind = "clip" if args.clip_id is not None else "goal"
    try:
        ids = _collect_ids(args.clip_id if id_kind == "clip" else args.goal_id)
    except ValueError as exc:
        print(json.dumps({"status": "error", "message": f"Could not parse ids: {exc}"}))
        sys.exit(1)

    if args.out is not None and len(ids) > 1:
        print(json.dumps({"status": "error", "message": "--out only supports a single id; use --out-dir for a batch"}))
        sys.exit(1)

    try:
        policy_key = clip_resolver.get_policy_key()
    except Exception as exc:
        print(json.dumps({"status": "error", "message": f"Could not fetch Brightcove policy key: {exc}"}))
        sys.exit(1)

    conn = db.connect() if id_kind == "goal" else None
    cursor = conn.cursor() if conn else None

    any_failed = False
    for id_value in ids:
        result = process_one(cursor, id_kind, id_value, args.type, args.out, args.out_dir, args.overwrite, policy_key)
        if result.get("status") == "error":
            any_failed = True
        print(json.dumps(result), flush=True)

    sys.exit(1 if any_failed else 0)


if __name__ == "__main__":
    main()

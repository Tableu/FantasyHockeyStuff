"""JSON files written whole or not at all: to a .tmp beside them, then swapped in (os.replace), so
a reader never sees half a file. `update` is a read-modify-write under one lock per file, for files
two parts of the server change (Settings/secrets.json: auth's user_secret, league_secrets' key)."""

import json
import os
import threading
from pathlib import Path

_locks: dict = {}
_locks_lock = threading.Lock()


def _lock(path: Path) -> threading.RLock:
    key = str(Path(path).resolve())
    with _locks_lock:
        return _locks.setdefault(key, threading.RLock())


def write(path: Path, data, indent: int = 1) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=indent, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def update(path: Path, change, indent: int = 2):
    """change(data) -> (data to write or None to leave the file, value to return)."""
    with _lock(path):
        data = read(path, {})
        new, value = change(data)
        if new is not None:
            write(path, new, indent)
        return value

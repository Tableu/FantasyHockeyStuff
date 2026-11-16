"""The site's users: one per browser (its fh_user cookie, auth.py), each the leagues it added.
No accounts and no recovery -- a browser whose cookie is gone starts again (the user, 2026-11-15).

    state/users/<uid>.json   {"id", "created_at", "last_seen", "leagues": [league names]}

A league is the registry's (leagues.py); a user is linked to it, and two users who add the same
team share it (its plan and its choices). Every user is treated the same: the hand-made leagues in
Settings/leagues/ are linked like any other, and only ever unlinked, never deleted.

Cleanup (the daily pass, server.py): users unseen for USER_DAYS go, and with them every added
league no user links any more (leagues.delete).
"""

import datetime as dt
import json
import secrets
import threading
from pathlib import Path

import jsonfile
import leagues

USER_DAYS = 60          # a user unseen this long is deleted
ACTIVE_DAYS = 14        # a league none of whose users came by in this long leaves the daily pass
_lock = threading.RLock()


def folder() -> Path:
    return leagues.state_root() / "users"


def _path(uid: str) -> Path:
    return folder() / f"{uid}.json"


def valid_uid(uid) -> bool:
    return isinstance(uid, str) and 16 <= len(uid) <= 64 and all(c.isalnum() or c in "-_" for c in uid)


def _write(user: dict) -> dict:
    jsonfile.write(_path(user["id"]), user)
    return user


def load(uid: str | None) -> dict | None:
    if not valid_uid(uid):
        return None
    try:
        return json.loads(_path(uid).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def create() -> dict:
    today = dt.date.today().isoformat()
    with _lock:
        return _write({"id": secrets.token_urlsafe(16), "created_at": today, "last_seen": today, "leagues": []})


def everyone() -> list:
    out = []
    for path in sorted(folder().glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except ValueError:
            continue
    return out


def linked(uid: str | None) -> list:
    """The user's leagues, in the order added."""
    user = load(uid)
    return list(user["leagues"]) if user else []


def link(uid: str, name: str) -> dict:
    with _lock:
        user = load(uid)
        if name not in user["leagues"]:
            user["leagues"].append(name)
        return _write(user)


def unlink(uid: str, name: str) -> dict:
    with _lock:
        user = load(uid)
        user["leagues"] = [n for n in user["leagues"] if n != name]
        return _write(user)


def touch(user: dict) -> bool:
    """last_seen = today on a user as read this request, written at most once a day: True when
    written (the first call of the day; auth renews the cookie then)."""
    today = dt.date.today().isoformat()
    if user.get("last_seen") == today:
        return False
    with _lock:
        fresh = load(user["id"])
        if fresh is None or fresh.get("last_seen") == today:
            return False
        fresh["last_seen"] = today
        _write(fresh)
        return True


def linked_by_anyone() -> set:
    return {name for user in everyone() for name in user["leagues"]}


def active_leagues(today: dt.date, days: int = ACTIVE_DAYS) -> list:
    """The leagues linked by a user seen in the last `days` (the daily pass plans these), in
    first-linked order."""
    since = (today - dt.timedelta(days=days)).isoformat()
    names = {}
    for user in everyone():
        if user.get("last_seen", "") >= since:
            names.update(dict.fromkeys(user["leagues"]))
    return list(names)


def drop_league(name: str) -> bool:
    """`name`'s files, if it is an added league no user links any more. True when deleted."""
    with _lock:
        if name in linked_by_anyone():
            return False
        try:
            league = leagues.load(name)
        except (SystemExit, ValueError):
            return False
        if not leagues.is_added(league):
            return False
        leagues.delete(league)
        return True


def cleanup(today: dt.date) -> list:
    """Users unseen for USER_DAYS deleted, then every added league nobody links. Returns what went."""
    since = (today - dt.timedelta(days=USER_DAYS)).isoformat()
    gone = []
    with _lock:
        for user in everyone():
            if user.get("last_seen", "") < since:
                _path(user["id"]).unlink(missing_ok=True)
                gone.append(f"user {user['id'][:6]}…")
        kept = linked_by_anyone()
        for path in sorted(leagues.state_dir().glob("*.json")):
            if path.stem not in kept and drop_league(path.stem):
                gone.append(f"league {path.stem}")
    return gone
